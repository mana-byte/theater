"""Command line entry point.

Three audiences, one binary:

    theater daemon      the singleton, usually started implicitly by a client
    theater mcp         the per-agent stdio MCP server, started by a harness
    theater ls|spawn    a human at a terminal
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from theater import __version__, config, paths
from theater import harness as harness_registry
from theater.client import DaemonClient, call_sync
from theater.formatting import (
    TIER_LEGEND,
    clip_harness,
    clip_name,
    event_stamp,
    event_summary,
    event_who,
    flatten_tree,
    pad_to_width,
    reach_mark,
    tier_mark,
    tilde,
)
from theater.harness import APPROVALS, HARNESSES, describe, harness_icon
from theater.protocol import RemoteError
from theater.tmux import client as tmux


class BadUsage(Exception):
    """The command line is wrong in a way argparse cannot express.

    Raised for the cases that depend on state argparse never sees — chiefly
    config. Caught in `main` and printed like any other usage error, so these
    do not read as crashes.
    """


#: Home, then erase. Cheaper than curses and good enough for a redraw loop.
_CLEAR = "\033[H\033[2J"

#: How many events to pull per follow tick. Larger than any plausible quarter
#: second of traffic, so the gap warning below stays theoretical.
_FOLLOW_BATCH = 200


def _add_models_parser(sub) -> None:
    """Register `theater models`.

    Its own function because `_parser` sits at the statement cap the linter
    enforces, and one flat registry of every flag was always going to reach it.
    New subcommands get a builder like this one rather than another few lines
    there.
    """
    models = sub.add_parser("models", help="Show, or discover, the models a spawn may name.")
    models.add_argument(
        "--discover",
        metavar="HARNESS",
        default=None,
        help=(
            "Ask that CLI what models it can run and print a [models] block "
            "to paste. Not every CLI can be asked."
        ),
    )
    models.add_argument("--json", action="store_true")


def _add_name_parser(sub) -> None:
    """Register small participant-targeting commands, extracted for the same
    reason as `models`: ``_parser`` sits at the statement cap the linter
    enforces.
    """
    kill = sub.add_parser(
        "kill",
        help="Kill a participant's pane. Use the id for destructive actions.",
    )
    kill.add_argument(
        "id",
        help=(
            "Participant id or name. Names are live-only and recyclable; a "
            "dead participant has no name. Use the id for destructive "
            "targeting — a recycled name can point at a successor."
        ),
    )

    name = sub.add_parser("name", help="Rename a participant.")
    name.add_argument(
        "target",
        help="Participant id or current name (live participants only).",
    )
    name.add_argument("new_name", help="The new name.")

    candidates = sub.add_parser(
        "candidates",
        help="List transcript candidates for operator recovery.",
    )
    candidates.add_argument("id", help="Stable participant id or live name.")
    candidates.add_argument("--json", action="store_true")

    bind = sub.add_parser(
        "bind",
        help="Bind a participant to a transcript candidate (same-UID operator authority).",
        description=(
            "Bind a participant to a transcript candidate. This is CLI/operator RPC "
            "authority, the same trust boundary as `theater kill`: it is not human "
            "authentication. A same-user process that can run theater or speak the "
            "daemon socket can invoke it. The stable-id confirmation prevents "
            "accidental alias-based binding."
        ),
    )
    bind.add_argument("id", help="Stable participant id or live name.")
    bind.add_argument("candidate", help="Candidate path or session URI from `theater candidates`.")
    bind.add_argument(
        "--confirm-id",
        required=True,
        help="Must exactly equal the stable target participant id.",
    )
    bind.add_argument(
        "--transfer-from",
        default=None,
        help="Current owner stable participant id, required to transfer an owned candidate.",
    )
    bind.add_argument(
        "--transfer-confirm-id",
        default=None,
        help="Must exactly equal --transfer-from when transferring ownership.",
    )


def _add_gc_parser(sub) -> None:
    """Register `theater gc`, extracted for the same reason as `models` and
    `name`: ``_parser`` sits at the statement cap the linter enforces.
    """
    gc = sub.add_parser("gc", help="Sweep old data from the database now.")
    gc.add_argument(
        "--vacuum",
        action="store_true",
        help=(
            "Rewrite the entire database file to reclaim disk space. "
            "Takes an exclusive lock for the duration — the daemon blocks."
        ),
    )
    gc.add_argument("--json", action="store_true")


def _add_receipt_parser(sub) -> None:
    """Register the hidden hook ingestion commands.

    ``transcript-receipt`` is the generic entry point: it reads a token file,
    parses JSON from stdin, and forwards ``id``/``token``/``payload`` to the
    ``transcript.receipt`` RPC with the payload untouched.

    ``claude-receipt`` is the backward-compatible alias shipped in v3.2.0.
    Live Claude sessions have ``settings.json`` on disk invoking it by that
    exact name, so it must keep working: it extracts ``session_id`` and
    ``transcript_path`` from the stdin JSON and wraps them into a payload
    before forwarding to ``transcript.receipt``.
    """
    receipt = sub.add_parser("transcript-receipt", help=argparse.SUPPRESS)
    receipt.add_argument("--id", required=True)
    receipt.add_argument("--token-file", required=True)

    alias = sub.add_parser("claude-receipt", help=argparse.SUPPRESS)
    alias.add_argument("--id", required=True)
    alias.add_argument("--token-file", required=True)


def _add_process_parsers(sub) -> None:
    """Register daemon-side process entry points."""
    daemon = sub.add_parser("daemon", help="Run the registry daemon in the foreground.")
    daemon.add_argument("--log-level", default=os.environ.get("THEATER_LOG_LEVEL", "INFO"))
    daemon.add_argument(
        "--timing",
        action="store_true",
        default=os.environ.get("THEATER_TIMING", "") not in ("", "0"),
        help=(
            "Log every timed operation, not just the slow ones. The env var "
            "THEATER_TIMING=1 does the same and reaches a daemon that autostarted "
            "itself, which the flag cannot."
        ),
    )

    mcp = sub.add_parser("mcp", help="Run the per-agent MCP server on stdio.")
    mcp.add_argument("--id", dest="participant_id", default=None)
    mcp.add_argument("--harness", default="unknown")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="theater",
        description="Cross-harness orchestration for coding agents.",
    )
    # Sits above the required subcommand: `theater --version` prints and exits
    # during parsing, before argparse checks that a subcommand was given.
    p.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"theater {__version__}",
    )
    sub = p.add_subparsers(dest="command", required=True)

    _add_process_parsers(sub)
    _add_receipt_parser(sub)

    ls = sub.add_parser("ls", help="List participants.")
    once = ls.add_mutually_exclusive_group()
    once.add_argument("--json", action="store_true")
    once.add_argument("--watch", action="store_true", help="Redraw until interrupted.")
    ls.add_argument(
        "--all",
        action="store_true",
        help=(
            "Include dead participants. Dead rows have no name (shown as '-'); "
            "use the id for historical access (retention-bounded)."
        ),
    )
    ls.add_argument("--tree", action="store_true", help="Show lineage.")
    ls.add_argument("--interval", type=float, default=1.0, help="Seconds per redraw.")

    spawn = sub.add_parser("spawn", help="Start an agent in a new tmux window.")
    # No `choices`: the legal set includes harnesses declared in config, which
    # is not read until after the parser is built. `_spawn_harness` validates
    # against the registry as it actually is.
    spawn.add_argument("harness", nargs="?", default=None)
    spawn.add_argument("prompt", nargs="?", default="")
    spawn.add_argument(
        "--prompt",
        dest="prompt_flag",
        default=None,
        help="The prompt, when no harness is named and the favourite is used.",
    )
    spawn.add_argument(
        "--approval",
        choices=APPROVALS,
        required=True,
        help="Tool approval policy for the new agent. No default, on purpose.",
    )
    spawn.add_argument("--cwd", default=None)
    spawn.add_argument("--parent", dest="parent_id", default=None)
    spawn.add_argument(
        "--model",
        default=None,
        help=(
            "Model for the new agent, spelled as its harness expects. "
            "Not validated: an unknown name fails in the pane, not here."
        ),
    )
    spawn.add_argument(
        "--reasoning-effort",
        default=None,
        help=(
            "Reasoning effort for the new agent (e.g. low, medium, high). "
            "Not validated: an unknown value fails in the pane, not here."
        ),
    )
    spawn.add_argument(
        "--worktree",
        nargs="?",
        const=True,
        default=False,
        type=str,
        help=(
            "Create a git worktree for the child. Bare --worktree creates an "
            "isolated worktree (unique index and HEAD). --worktree NAME creates "
            "or joins a named shared linked worktree — multiple children with "
            "the same name share one directory and branch. Expert mode: the "
            "index and HEAD are shared, concurrent git add/commit operations "
            "can interfere, and Theater does not enforce file ownership."
        ),
    )
    spawn.add_argument(
        "--base-branch",
        default=None,
        help="Base branch for the worktree. Defaults to current HEAD.",
    )
    spawn.add_argument(
        "--foreground",
        action="store_true",
        help="Switch to the new window instead of leaving it in the background.",
    )
    spawn.add_argument("--json", action="store_true")

    bus = sub.add_parser("bus", help="Show the normalized event feed.")
    bus.add_argument("-f", "--follow", action="store_true")
    bus.add_argument("-n", "--limit", type=int, default=50)
    bus.add_argument(
        "--kind",
        default=None,
        help="Only events whose kind starts with this, e.g. 'agent.' or 'agent.tool'.",
    )
    bus.add_argument("--json", action="store_true", help="One JSON object per line.")
    bus.add_argument("--interval", type=float, default=0.4, help="Seconds per poll.")

    adopt = sub.add_parser(
        "adopt",
        help="Adopt the pane you are running in as a Theater participant.",
    )
    adopt.add_argument(
        "--harness",
        default=None,
        help=(
            "Override harness detection. By default the pane's current command "
            "is matched against known harness binaries."
        ),
    )
    adopt.add_argument("--json", action="store_true")

    harnesses = sub.add_parser("harnesses", help="List the coding CLIs Theater knows how to drive.")
    harnesses.add_argument("--json", action="store_true")

    conf = sub.add_parser("config", help="Show resolved settings and where they came from.")
    conf.add_argument(
        "topic",
        nargs="?",
        choices=["path"],
        default=None,
        help="'path' prints the config file location and nothing else.",
    )
    conf.add_argument("--json", action="store_true")

    _add_models_parser(sub)
    _add_name_parser(sub)
    _add_gc_parser(sub)

    stats = sub.add_parser("stats", help="How turns have been ending, per harness.")
    stats.add_argument(
        "--window",
        type=float,
        default=None,
        metavar="HOURS",
        help=(
            "Only turns started in the last N hours. Default: all retained "
            "history — retention is finite, so older rows may be gone."
        ),
    )
    stats.add_argument("--json", action="store_true")

    sub.add_parser("regie", help="Launch the régie TUI (run inside tmux).")

    sub.add_parser("stop", help="Shut the daemon down.")

    sub.add_parser(
        "restart",
        help="Restart the daemon, applying config changes. Agents keep running.",
    )

    return p


# ---- commands ----------------------------------------------------------


def cmd_daemon(args) -> int:
    from theater import timing
    from theater.daemon.server import run

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    if getattr(args, "timing", False):
        timing.enable_trace()
    try:
        asyncio.run(run())
    except RuntimeError as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


def cmd_mcp(args) -> int:
    from theater.mcp.server import main

    main(args.participant_id, args.harness)
    return 0


def _hook_string(data: dict, *names: str) -> str | None:
    for name in names:
        value = data.get(name)
        if isinstance(value, str) and value:
            return value
    return None


async def _send_transcript_receipt(args, *, token: str, payload: dict) -> None:
    async with DaemonClient(autostart=False) as client:
        await client.call(
            "transcript.receipt",
            id=args.id,
            token=token,
            payload=payload,
        )


def cmd_transcript_receipt(args) -> int:
    """Ingest a lifecycle hook receipt and forward the payload untouched.

    Hidden from normal CLI help. The hook provides JSON on stdin; the launch
    token lives in a daemon-written file so it is not exposed in participant
    rows, transcripts, or argv.
    """
    try:
        token = Path(args.token_file).read_text().strip()
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        asyncio.run(_send_transcript_receipt(args, token=token, payload=payload))
    except (RemoteError, ConnectionError, OSError):
        return 0
    return 0


def cmd_claude_receipt(args) -> int:
    """Backward-compatible alias for the Claude-specific hook command.

    Shipped in v3.2.0; live Claude sessions have settings.json invoking
    ``claude-receipt`` by that exact name. Extracts ``session_id`` and
    ``transcript_path`` from stdin JSON and wraps them into a payload before
    forwarding to the generic ``transcript.receipt`` RPC.
    """
    try:
        token = Path(args.token_file).read_text().strip()
        payload = json.load(sys.stdin)
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = _hook_string(payload, "session_id", "sessionId")
    transcript_path = _hook_string(payload, "transcript_path", "transcriptPath")
    if session_id is None or transcript_path is None:
        return 0
    forwarded = {"session_id": session_id, "transcript_path": transcript_path}
    try:
        asyncio.run(_send_transcript_receipt(args, token=token, payload=forwarded))
    except (RemoteError, ConnectionError, OSError):
        return 0
    return 0


def _width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _row_line(p: dict, indent: int = 0) -> str:
    pad = "  " * indent
    return (
        f"{p['id']:<14}{tier_mark(p['tier'])}{reach_mark(p['addressable'])} "
        f"{clip_name(p.get('name')):<12} "
        f"{harness_icon(p.get('harness'))} "
        f"{clip_harness(p.get('harness')):<11} "
        f"{p['status']:<15} {p.get('tmux_pane') or '-':<6} {pad}{tilde(p.get('cwd'))}"
    )


def _format_ls(rows: list[dict], *, tree: bool, unmanaged: list[dict] | None = None) -> str:
    if not rows and not unmanaged:
        return "no participants"
    body = flatten_tree(rows, _row_line) if tree else [_row_line(r) for r in rows]
    header = (
        f"{'ID':<14}{'T':<2} {'NAME':<12}   {'HARNESS':<11} {'STATUS':<15} {'PANE':<6} DIRECTORY"
    )
    lines = [header, *body]
    if unmanaged:
        lines.append("")
        lines.append("unmanaged (harness panes not yet adopted):")
        for u in unmanaged:
            cmd = clip_harness(u.get("command"))
            pane = u.get("pane") or "-"
            icon = harness_icon(u.get("harness") or u.get("command"))
            lines.append(
                f"  {'-':<12}{'?':<2} {'-':<12} {icon} {cmd:<11} "
                f"{'-':<15} {pane:<6} {tilde(u.get('cwd'))}"
            )
    lines.extend(["", TIER_LEGEND])
    return "\n".join(lines)


async def _watch_ls(args, method: str, params: dict) -> int:
    async with DaemonClient() as client:
        while True:
            rows = await client.call(method, **params)
            assert isinstance(rows, list)
            unmanaged: list = []
            if not args.tree:
                found = await client.call("participants.unmanaged")
                assert isinstance(found, list)
                unmanaged = found
            stamp = time.strftime("%H:%M:%S")
            frame = _format_ls(rows, tree=args.tree, unmanaged=unmanaged or None)
            sys.stdout.write(f"{_CLEAR}{stamp}  (ctrl-c to stop)\n\n{frame}\n")
            sys.stdout.flush()
            await asyncio.sleep(args.interval)


def cmd_ls(args) -> int:
    method = "participants.tree" if args.tree else "participants.list"
    params: dict = {} if args.tree else {"include_dead": args.all}
    if args.watch:
        return asyncio.run(_watch_ls(args, method, params))
    rows = call_sync(method, **params)
    assert isinstance(rows, list)
    unmanaged: list[dict] | None = None
    if not args.tree:
        unmanaged = call_sync("participants.unmanaged")  # type: ignore[assignment]
        assert isinstance(unmanaged, list)
    if args.json:
        print(json.dumps({"participants": rows, "unmanaged": unmanaged or []}, indent=2))
        return 0
    print(_format_ls(rows, tree=args.tree, unmanaged=unmanaged))
    return 0


def _spawn_harness(args) -> str:
    """The harness to spawn: the one named, else the configured favourite.

    An unset favourite is not a silent fallback to some arbitrary harness —
    picking one for the user is exactly the guess this release is trying not
    to make — so it is an error that says how to fix itself.
    """
    known = ", ".join(sorted(HARNESSES))
    if args.harness:
        if args.harness not in HARNESSES:
            # Validated here, not by argparse `choices`: declared harnesses are
            # not in the registry when the parser is built.
            raise BadUsage(
                f"unknown harness {args.harness!r} (known: {known}). If you "
                "meant this as the prompt, pass it with --prompt."
            )
        return args.harness
    favourite = config.load().theater.favourite
    if not favourite:
        raise BadUsage(
            "no harness given and no favourite set — name one "
            f"({known}), or set theater.favourite in {paths.config_path()}"
        )
    if favourite not in HARNESSES:
        raise BadUsage(
            f"theater.favourite is {favourite!r}, which is not a known harness ({known})"
        )
    return favourite


def cmd_spawn(args) -> int:
    harness = _spawn_harness(args)
    record = call_sync(
        "spawn",
        harness=harness,
        prompt=args.prompt_flag if args.prompt_flag is not None else args.prompt,
        approval=args.approval,
        cwd=args.cwd or str(Path.cwd()),
        parent_id=args.parent_id,
        tmux_session=tmux.current_session_sync(),
        background=not args.foreground,
        worktree=args.worktree,
        base_branch=args.base_branch,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        assert isinstance(record, dict)
        print(f"{record['id']}  {record['harness']}  pane {record['tmux_pane']}")
    return 0


def _bus_line(row: dict, width: int) -> str:
    line = (
        f"{event_stamp(row.get('ts'))}  {row.get('kind', '?'):<18} "
        f"{event_who(row):<32} {event_summary(row.get('payload'))}"
    )
    return line[: width - 1] if width > 20 and len(line) >= width else line


def _emit_bus(rows: list[dict], args) -> None:
    width = _width()
    for row in rows:
        print(json.dumps(row) if args.json else _bus_line(row, width))
    # Python block-buffers stdout when it is not a terminal, so without this a
    # followed feed piped into anything produces nothing until the buffer fills
    # or the process dies — indistinguishable from a hang.
    sys.stdout.flush()


def _matching(rows: list[dict], prefix: str | None) -> list[dict]:
    if not prefix:
        return rows
    return [r for r in rows if str(r.get("kind", "")).startswith(prefix)]


async def _follow_bus(args) -> int:
    async with DaemonClient() as client:
        rows = await client.call("bus.tail", limit=args.limit)
        assert isinstance(rows, list)
        cursor = rows[-1]["id"] if rows else 0
        _emit_bus(_matching(rows, args.kind), args)
        while True:
            await asyncio.sleep(args.interval)
            rows = await client.call("bus.tail", limit=_FOLLOW_BATCH, after_id=cursor)
            assert isinstance(rows, list)
            if not rows:
                continue
            # bus.tail returns the *newest* `limit` rows after the cursor, so a
            # burst larger than the batch silently loses the middle. Ids are
            # contiguous, so the gap is exactly measurable — say so rather than
            # letting the feed quietly lie about being complete.
            missed = rows[0]["id"] - cursor - 1
            if missed > 0:
                print(f"... {missed} events dropped (feed fell behind)")
            cursor = rows[-1]["id"]
            _emit_bus(_matching(rows, args.kind), args)


def cmd_bus(args) -> int:
    if args.follow:
        return asyncio.run(_follow_bus(args))
    rows = call_sync("bus.tail", limit=args.limit)
    assert isinstance(rows, list)
    rows = _matching(rows, args.kind)
    if not rows and not args.json:
        print("no events")
        return 0
    _emit_bus(rows, args)
    return 0


def cmd_kill(args) -> int:
    call_sync("participant.kill", id=args.id)
    print(f"killed {args.id}")
    return 0


def cmd_name(args) -> int:
    record = call_sync("participant.rename", id=args.target, name=args.new_name)
    assert isinstance(record, dict)
    print(f"renamed {args.target} -> {record.get('name')}")
    return 0


def _candidate_line(row: dict) -> str:
    owner = row.get("owner") or "-"
    tombstone = row.get("tombstone") or "-"
    reason = row.get("rejection_reason") or "-"
    size = row.get("size")
    mtime = row.get("mtime")
    stamp = event_stamp(mtime) if mtime else "-"
    size_text = _format_bytes(size) if isinstance(size, int) else "-"
    return (
        f"{row.get('location'):<72} {row.get('session_id') or '-':<36} "
        f"{stamp:<17} {size_text:>9} {owner:<14} {tombstone:<14} {reason}"
    )


def cmd_candidates(args) -> int:
    data = call_sync("transcript.candidates", id=args.id)
    assert isinstance(data, dict)
    rows = data.get("candidates") or []
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    if not rows:
        print("no candidates")
        return 0
    print(
        f"{'LOCATION':<72} {'SESSION':<36} {'MTIME':<17} {'SIZE':>9} "
        f"{'OWNER':<14} {'TOMBSTONE':<14} REJECTION"
    )
    for row in rows:
        print(_candidate_line(row))
    return 0


def cmd_bind(args) -> int:
    record = call_sync(
        "transcript.bind",
        id=args.id,
        candidate=args.candidate,
        confirm_id=args.confirm_id,
        transfer_from=args.transfer_from,
        transfer_confirm_id=args.transfer_confirm_id,
    )
    assert isinstance(record, dict)
    prior = f" (transferred from {record['prior_owner']})" if record.get("prior_owner") else ""
    print(f"bound {record['id']} -> {record['location']}{prior}")
    return 0


def cmd_adopt(args) -> int:
    """Adopt the caller's own pane — no model in the loop.

    The user runs `theater adopt` from inside a hand-started agent session.
    The pane id comes from $TMUX_PANE; the harness is detected from the pane's
    current command, unless overridden. The daemon does the tmux lookup, because
    it has tmux access and the CLI process may not have the venv's PATH.
    """
    pane = tmux.current_pane()
    if pane is None:
        print(
            "theater: adopt needs $TMUX_PANE — run this from inside a tmux pane",
            file=sys.stderr,
        )
        return 1
    record = call_sync("adopt", pane=pane, harness=args.harness, cwd=str(Path.cwd()))
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        assert isinstance(record, dict)
        mark = tier_mark(record["tier"])
        print(f"{record['id']}  {mark} {record['harness']}  pane {record['tmux_pane']}")
    return 0


def _harness_rows() -> tuple[list[dict], str | None]:
    """The harness list, and why it is not the daemon's answer if it is not.

    The daemon is authoritative — it is the process that will refuse a spawn,
    and it holds the config as of its own start. But it is asked with autostart
    off: `theater harnesses` answers "what can I pass to spawn", which must
    work before anything else does and must not be the thing that launches a
    daemon. With none running, the local registry is the same answer anyway,
    because the daemon would read the same config when it starts.
    """

    async def go():
        async with DaemonClient(autostart=False) as client:
            return await client.call("harnesses")

    try:
        rows = asyncio.run(go())
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError):
        return describe(), "no daemon running"
    except RemoteError as exc:
        # A daemon older than this CLI has no such method, and one that
        # predates it necessarily has the built-in registry — so the local
        # answer is right. Anything else is a real fault and must not be
        # papered over with a plausible-looking list.
        if exc.code != "unknown_method":
            raise
        return describe(), "the running daemon predates this command"
    assert isinstance(rows, list)
    return rows, None


def cmd_harnesses(args) -> int:
    """List the harness adapters, as the daemon sees them."""
    rows, fallback = _harness_rows()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{pad_to_width('', 2)} {'NAME':<10} {'SOURCE':<8} {'BINARY':<10} {'INSTALLED':<10} PATH")
    for r in rows:
        # A plugin that would not load has no binary to look for, so
        # "installed: no" would blame PATH for a syntax error. Say broken.
        mark = "broken" if r.get("error") else ("yes" if r["installed"] else "no")
        print(
            f"{pad_to_width(r['icon'], 2)} {r['name']:<10} {r.get('source', '-'):<8} "
            f"{r['binary'] or '-':<10} {mark:<10} "
            f"{tilde(r['path']) if r['path'] else '-'}"
        )
    missing = [r["name"] for r in rows if not r["installed"] and not r.get("error")]
    if missing:
        print(f"\nnot on PATH: {', '.join(missing)} — spawn will refuse these")
    for r in rows:
        if r.get("error"):
            print(f"\n{r['name']}: {r['error']}")
    if fallback:
        # Worth saying which list this is: a daemon already up may be holding
        # a different one, and only it can refuse a spawn.
        print(f"\n{fallback} — read from this process's registry")
    return 0


def _format_floor(ts: float | None) -> str:
    """Render a retention-floor timestamp, or say plainly that there is none."""
    if ts is None:
        return "no data"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _print_coverage(data: dict, args) -> None:
    """Show how far back the data actually goes.

    RESCUED exists to surface silent degradation, so the window it was counted
    over cannot itself be silent: "0 rescued" over a week of retained history
    means something different from "0 rescued" over all time. Jobs and bus
    events sit in different tables under different retention, so each gets its
    own floor.
    """
    coverage = data.get("coverage") or {}
    jobs_from = coverage.get("jobs_from")
    bus_from = coverage.get("bus_from")

    print()
    print(f"coverage: jobs from {_format_floor(jobs_from)}")
    print(f"          bus from {_format_floor(bus_from)}")

    if args.window is None:
        return
    requested = time.time() - float(args.window) * 3600.0
    asked = time.strftime("%Y-%m-%d %H:%M", time.localtime(requested))
    for label, floor in (("jobs", jobs_from), ("bus", bus_from)):
        if floor is not None and requested < floor:
            print(f"          asked back to {asked} — {label} data starts later than the window")


def cmd_stats(args) -> int:
    """How turns have been ending, per harness.

    The number that matters is RESCUED: a turn the observer never saw end, so
    the daemon waited out the rescue timer and handed the caller the last thing
    the agent was heard to say. The caller cannot tell that apart from a real
    answer — it reads as a slightly odd reply, or a slow one — so without this
    command a harness whose transcript format has drifted degrades invisibly.

    A high rate for one harness is a parser problem. A high rate everywhere is
    a problem with how turn ends are matched to jobs.
    """
    data = call_sync("stats", window=args.window)
    assert isinstance(data, dict)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    rows = data.get("harnesses") or []
    if not rows:
        print("no turns recorded yet")
        _print_coverage(data, args)
        return 0

    print(
        f"{'HARNESS':<12} {'TURNS':>6} {'CLEAN':>6} {'RESCUED':>8} "
        f"{'FAILED':>7} {'RUNNING':>8}  RESCUE RATE"
    )
    for r in rows:
        finished = (r["clean"] or 0) + (r["rescued"] or 0)
        # Against finished turns, not all turns: one still running is not yet
        # evidence either way, and counting it as clean would make a burst of
        # activity look like an improvement.
        rate = f"{100 * r['rescued'] / finished:.0f}%" if finished else "-"
        print(
            f"{clip_harness(r['harness'], 12):<12} {r['turns']:>6} {r['clean']:>6} "
            f"{r['rescued']:>8} {r['failed']:>7} {r['running']:>8}  {rate:>11}"
        )

    refusals = data.get("refusals") or {}
    if refusals:
        listed = ", ".join(f"{k} {v}" for k, v in sorted(refusals.items()))
        print(f"\nrefused before delivery: {listed}")
    _print_coverage(data, args)
    return 0


def _format_bytes(n: int) -> str:
    """A human-readable byte count, because raw bytes are unreadable on a CLI."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(size) < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def cmd_gc(args) -> int:
    """Run a garbage-collection sweep now and report what was removed.

    Deleting rows does not shrink the database file — only ``--vacuum``
    does, by rewriting it under an exclusive lock. Without saying that, a
    user who runs ``theater gc`` and checks ``ls -l theater.db`` will report
    GC as broken.
    """
    data = call_sync("gc", vacuum=args.vacuum)
    assert isinstance(data, dict)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    bus = data.get("bus", 0)
    jobs = data.get("jobs", 0)
    touch = data.get("touch", 0)
    participants = data.get("participants", 0)
    running_marked = data.get("running_marked", 0)
    scratchpad = data.get("scratchpad", 0)
    total = bus + jobs + touch + participants + running_marked + scratchpad

    if total == 0:
        print("nothing to collect — database is already within retention")
    else:
        print(
            f"collected: {bus} bus, {jobs} jobs, {touch} touch, "
            f"{participants} participants, {running_marked} stale running marked, "
            f"{scratchpad} scratchpad"
        )

    coverage = data.get("coverage") or {}
    print()
    print(f"coverage: jobs from {_format_floor(coverage.get('jobs_from'))}")
    print(f"          bus from {_format_floor(coverage.get('bus_from'))}")

    before = data.get("db_bytes_before", 0)
    after = data.get("db_bytes_after", 0)
    print(f"\ndatabase: {_format_bytes(before)} -> {_format_bytes(after)}")

    vacuum_ran = data.get("vacuum_ran", False)
    if vacuum_ran:
        reclaimed = before - after
        if reclaimed > 0:
            print(f"vacuum reclaimed {_format_bytes(reclaimed)}")
        else:
            print("vacuum ran — file size unchanged (nothing to reclaim)")
    elif total > 0:
        # The single most important line in this command: without it, a user
        # who deleted 94% of the database and saw the file not shrink will
        # report GC as broken.
        print(
            "file size unchanged — deleting rows does not shrink the file; "
            "use `theater gc --vacuum` to reclaim space"
        )
    return 0


def cmd_config(args) -> int:
    """Show the resolved settings, each tagged with where it came from.

    Prints the *resolved* view rather than the file, because the question this
    answers is "did my edit take effect", which the file cannot answer. Like
    `harnesses`, it reads local data and never contacts the daemon — a config
    error is exactly the thing that stops the daemon from starting, so this has
    to work when nothing else does.

    A malformed file is reported here as the daemon would reject it, so the
    same message is available before the first confusing start-up failure.
    """
    if args.topic == "path":
        print(paths.config_path())
        return 0

    try:
        loaded = config.load()
    except config.ConfigError as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1

    rows = config.describe(loaded)
    if args.json:
        print(
            json.dumps(
                {
                    "path": str(loaded.path),
                    "exists": loaded.exists,
                    "settings": [
                        {"key": key, "value": value, "source": source}
                        for key, value, source in rows
                    ],
                },
                indent=2,
            )
        )
        return 0

    where = tilde(str(loaded.path))
    print(f"{where}{'' if loaded.exists else '  (no file yet — all defaults)'}\n")
    width = max(len(key) for key, _, _ in rows)
    for key, value, source in rows:
        mark = "" if source == "default" else "  <- config.toml"
        print(f"{key:<{width}}  {value}{mark}")
    if loaded.exists:
        print("\nchanges apply on `theater restart`")
    return 0


def _models_block(harness: str, models: list[str]) -> str:
    """Render a `[models]` entry the user can paste without editing.

    Model names are quoted with `json.dumps`, which is exact here: a TOML basic
    string and a JSON string agree on every escape that can appear in a model
    name, and both are double-quoted. Writing the quotes by hand would be wrong
    the first time a name contains a backslash.
    """
    lines = [f"[{config.MODELS_SECTION}]", f"{harness} = ["]
    lines += [f"  {json.dumps(name)}," for name in models]
    lines.append("]")
    return "\n".join(lines)


def cmd_models(args) -> int:
    """Show, or discover, the models a spawn may name for each harness.

    Two jobs, because they are two halves of one task. Bare, it answers "what
    will `--model` accept", which is a config question. With `--discover`, it
    asks the CLI itself what it can run and prints that as a config block —
    the on-ramp, since the allowlist starts empty and an empty list refuses
    every `--model`.

    Discovery is an authoring aid and never a gate: nothing here is consulted
    at spawn time, and the block is a suggestion the human edits down to the
    models they actually want spent on. Like `config`, this reads local data
    and never contacts the daemon — the allowlist is enforced from the file,
    so the file is the honest thing to report.
    """
    loaded = config.load()

    if args.discover:
        name = harness_registry.normalize(args.discover)
        adapter = HARNESSES.get(name)
        if adapter is None:
            known = ", ".join(sorted(HARNESSES))
            raise BadUsage(f"unknown harness {args.discover!r}; known: {known}")
        try:
            found = adapter.discover_models()
        except NotImplementedError as exc:
            # Not a failure of this run: the CLI has no way to be asked, and no
            # amount of retrying changes that. Say so, and point at the manual
            # route, which still works.
            print(f"theater: {exc}", file=sys.stderr)
            print(
                f"theater: list them by hand under [{config.MODELS_SECTION}] "
                f"in {tilde(str(loaded.path))}",
                file=sys.stderr,
            )
            return 1
        if not found:
            # Asked and answered: none. Distinct from the case above, and
            # usually a provider that is not logged in yet.
            print(
                f"theater: {name} reported no models — it may not be authenticated yet",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps({"harness": name, "models": found}, indent=2))
            return 0
        print(f"# {len(found)} found — paste into {tilde(str(loaded.path))},")
        print("# keeping only the models you want spawns to be able to name")
        print(_models_block(name, found))
        return 0

    if args.json:
        print(json.dumps(loaded.models, indent=2))
        return 0

    # Every known harness, not just the configured ones: the absent entries are
    # the point, since those are the harnesses `--model` currently refuses.
    names = sorted(set(HARNESSES) | set(loaded.models))
    if not names:
        print("no harnesses registered")
        return 0
    width = max(len(name) for name in names)
    print(f"{tilde(str(loaded.path))}\n")
    for name in names:
        allowed = loaded.models_for(name)
        listed = ", ".join(allowed) if allowed else "-  (--model refused)"
        print(f"{name:<{width}}  {listed}")
    print("\n`theater models --discover <harness>` prints a block to paste")
    return 0


#: How long to wait for a stopping daemon to let go. Bounded by the observer
#: stopping and connections draining, not by any work in flight — but those are
#: not instant, which is why the wait exists at all.
STOP_TIMEOUT = 5.0


def _shutdown_running_daemon() -> bool:
    """Ask a running daemon to stop. False when there was none to ask.

    Autostart off, which is not a detail: the previous version used the
    autostarting client, so `theater stop` with nothing running would launch a
    daemon purely to tell it to shut down.

    Connecting is what answers "is there a daemon"; the call is not. A daemon
    that shuts down promptly may cancel this very connection before its reply
    is drained, and reporting that as "no daemon running" told the user the
    opposite of what had just happened. So the connect is allowed to raise and
    the call is not.
    """

    async def go():
        async with DaemonClient(autostart=False) as client:
            await client.connect()
            with contextlib.suppress(ConnectionError, OSError):
                await client.call("shutdown")

    try:
        asyncio.run(go())
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError):
        return False
    return True


def _daemon_released() -> bool:
    """True once the old daemon holds neither the socket nor the lock.

    Both, because they answer different questions. The socket is what the next
    client connects to, so a leftover one means a replacement could reach the
    dying daemon. The lock is what the next daemon needs to take, and it is the
    only reliable signal: a daemon killed with -9 leaves its socket file behind
    forever but loses its lock the moment it dies.
    """
    from theater.daemon import lock

    return not paths.socket_path().exists() and lock.is_free()


def _await_daemon_gone(timeout: float | None = None) -> bool:
    """Wait for the stopping daemon to release what a replacement needs.

    The default is read at call time, not bound as a default argument, so the
    wait is patchable — otherwise a test for the timeout path has to take the
    full timeout.
    """
    deadline = time.monotonic() + (STOP_TIMEOUT if timeout is None else timeout)
    while not _daemon_released() and time.monotonic() < deadline:
        time.sleep(0.05)
    return _daemon_released()


def cmd_stop(args) -> int:
    if not _shutdown_running_daemon():
        print("no daemon running")
        return 0
    print("daemon stopping")
    return 0


def cmd_restart(args) -> int:
    """Stop the daemon and start a fresh one.

    This is how a config edit takes effect — config is read once at start and
    never reloaded. Nothing else is disturbed: agents live in tmux panes this
    process does not touch, and the registry is on disk, so the new daemon
    comes back to the same participants.
    """
    if _shutdown_running_daemon() and not _await_daemon_gone():
        held = paths.socket_path() if paths.socket_path().exists() else paths.pidfile_path()
        print(
            f"theater: daemon still holding {held} after {STOP_TIMEOUT:g}s "
            "— not starting a second one",
            file=sys.stderr,
        )
        return 1
    # Autostart does the starting, and waits for the socket with a real error
    # if it never appears. The ping is what makes "started" a fact.
    call_sync("ping")
    print("daemon restarted")
    return 0


def cmd_regie(args) -> int:
    """Launch the régie TUI.

    Must be run inside tmux: the régie is itself a tmux pane, and the stage
    is a real pane in the same window. If $TMUX is not set, the user needs
    to attach to a session first.
    """
    if not tmux.inside_tmux():
        print(
            "theater: regie must run inside tmux — attach to a session first",
            file=sys.stderr,
        )
        return 1
    settings = config.load()
    from theater.regie.app import run_regie

    run_regie(settings)
    return 0


_COMMANDS = {
    "daemon": cmd_daemon,
    "mcp": cmd_mcp,
    "transcript-receipt": cmd_transcript_receipt,
    "claude-receipt": cmd_claude_receipt,
    "ls": cmd_ls,
    "bus": cmd_bus,
    "spawn": cmd_spawn,
    "kill": cmd_kill,
    "name": cmd_name,
    "candidates": cmd_candidates,
    "bind": cmd_bind,
    "adopt": cmd_adopt,
    "harnesses": cmd_harnesses,
    "stats": cmd_stats,
    "gc": cmd_gc,
    "config": cmd_config,
    "models": cmd_models,
    "regie": cmd_regie,
    "stop": cmd_stop,
    "restart": cmd_restart,
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths.ensure_home()
    try:
        # Build the harness registry before the command runs, so `spawn`, `ls`
        # and `harnesses` all see the same set the daemon will.
        # `config` is exempt: it is the command built to explain a broken config
        # file, so it is the one that must still work without a usable one.
        if args.command != "config":
            harness_registry.install(config.load())
        return _COMMANDS[args.command](args)
    except (BadUsage, config.ConfigError) as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1
    except RemoteError as exc:
        print(f"theater: {exc.code}: {exc.message}", file=sys.stderr)
        return 1
    except ConnectionError as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # `ls --watch` and `bus -f` are meant to be ended this way. A traceback
        # would suggest something went wrong.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
