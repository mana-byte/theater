"""Command line entry point.

Three audiences, one binary:

    theater daemon      the singleton, usually started implicitly by a client
    theater mcp          the per-agent stdio MCP server, started by a harness
    theater ls|spawn    a human at a terminal

Parser construction lives in ``cli/parser.py``; pure formatting in
``cli/render.py``.  This module owns the command dispatch, shared seams
(``call_sync``, ``DaemonClient``, ``tmux``, ``HARNESSES``, ``STOP_TIMEOUT``)
that tests monkeypatch, and the ``main`` entry point.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil  # noqa: F401 — tests monkeypatch cli.shutil.which
import sys
import time
from pathlib import Path

from theater import config, paths
from theater import harness as harness_registry
from theater.cli.parser import _parser
from theater.cli.render import (
    _bus_line,
    _candidate_line,
    _format_bytes,
    _format_floor,
    _format_ls,
    _matching,
    _models_block,
    _width,
)
from theater.cli.render import (
    _row_line as _row_line,
)
from theater.client import DaemonClient, call_sync
from theater.formatting import clip_harness, pad_to_width, tier_mark, tilde
from theater.harness import HARNESSES, describe
from theater.harness import harness_icon as harness_icon
from theater.protocol import RemoteError
from theater.tmux import client as tmux


class BadUsage(Exception):
    """The command line is wrong in a way argparse cannot express."""


#: Home, then erase. Cheaper than curses and good enough for a redraw loop.
_CLEAR = "\033[H\033[2J"

#: How many events to pull per follow tick.
_FOLLOW_BATCH = 200

#: How long to wait for a stopping daemon to let go.
STOP_TIMEOUT = 5.0


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


def _emit_bus(rows: list[dict], args) -> None:
    width = _width()
    for row in rows:
        print(json.dumps(row) if args.json else _bus_line(row, width))
    # Flush so piped output is not block-buffered into a hang.
    sys.stdout.flush()


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
            # Validated here, not by argparse `choices`: harnesses not in registry at parse time.
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
            # bus.tail returns newest `limit` after cursor; ids contiguous, so gap is measurable.
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
        # A daemon older than this CLI has no such method.
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
        # A plugin that would not load has no binary to look for, so say broken.
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
        # Worth saying which list this is: a daemon already up may hold a different one.
        print(f"\n{fallback} — read from this process's registry")
    return 0


def _print_coverage(data: dict, args) -> None:
    """Show how far back the data actually goes."""
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
        # Against finished turns, not all turns: one still running is not yet evidence either way.
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
        # Without this line, a user who deleted 94% and saw no shrink reports GC as broken.
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
            # Not a failure of this run: the CLI has no way to be asked.
            print(f"theater: {exc}", file=sys.stderr)
            print(
                f"theater: list them by hand under [{config.MODELS_SECTION}] "
                f"in {tilde(str(loaded.path))}",
                file=sys.stderr,
            )
            return 1
        if not found:
            # Asked and answered: none; usually a provider that is not logged in yet.
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
        print(_models_block(name, found, config.MODELS_SECTION))
        return 0

    if args.json:
        print(json.dumps(loaded.models, indent=2))
        return 0

    # Every known harness, not just the configured ones: the absent entries are the point.
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
    # Autostart does the starting; the ping makes "started" a fact.
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
        # `ls --watch` and `bus -f` are meant to be ended this way.
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
