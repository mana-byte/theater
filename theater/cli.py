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

from theater import config, paths
from theater import harness as harness_registry
from theater.client import DaemonClient, call_sync
from theater.formatting import (
    TIER_LEGEND,
    clip_harness,
    event_stamp,
    event_summary,
    event_who,
    flatten_tree,
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


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="theater",
        description="Cross-harness orchestration for coding agents.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon", help="Run the registry daemon in the foreground.")
    daemon.add_argument(
        "--log-level", default=os.environ.get("THEATER_LOG_LEVEL", "INFO")
    )

    mcp = sub.add_parser("mcp", help="Run the per-agent MCP server on stdio.")
    mcp.add_argument("--id", dest="participant_id", default=None)
    mcp.add_argument("--harness", default="unknown")

    ls = sub.add_parser("ls", help="List participants.")
    once = ls.add_mutually_exclusive_group()
    once.add_argument("--json", action="store_true")
    once.add_argument(
        "--watch", action="store_true", help="Redraw until interrupted."
    )
    ls.add_argument("--all", action="store_true", help="Include dead participants.")
    ls.add_argument("--tree", action="store_true", help="Show lineage.")
    ls.add_argument("--interval", type=float, default=1.0, help="Seconds per redraw.")

    spawn = sub.add_parser("spawn", help="Start an agent in a new tmux window.")
    # Optional, and deliberately without `choices`: the legal set now includes
    # harnesses declared in config, which is not read until after the parser is
    # built. `_spawn_harness` validates instead, against the registry as it
    # actually is. A wrong name still fails loudly rather than being mistaken
    # for the prompt — which is why the prompt also has a flag form, since with
    # two optional positionals `theater spawn "do the thing"` would otherwise
    # bind the prompt to the harness slot and guess at what the user meant.
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
        "--worktree",
        action="store_true",
        help="Create a git worktree for the child with isolated index and HEAD.",
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

    kill = sub.add_parser("kill", help="Kill a participant's pane.")
    kill.add_argument("id")

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

    harnesses = sub.add_parser(
        "harnesses", help="List the coding CLIs Theater knows how to drive."
    )
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

    stats = sub.add_parser(
        "stats", help="How turns have been ending, per harness."
    )
    stats.add_argument(
        "--window",
        type=float,
        default=None,
        metavar="HOURS",
        help="Only turns started in the last N hours. Default: all of history.",
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
    from theater.daemon.server import run

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
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


def _width() -> int:
    return shutil.get_terminal_size((100, 24)).columns


def _row_line(p: dict, indent: int = 0) -> str:
    pad = "  " * indent
    return (
        f"{p['id']:<14}{tier_mark(p['tier'])}{reach_mark(p['addressable'])} "
        f"{harness_icon(p.get('harness'))} "
        f"{clip_harness(p.get('harness')):<11} "
        f"{p['status']:<15} {p.get('tmux_pane') or '-':<6} {pad}{tilde(p.get('cwd'))}"
    )


def _format_ls(rows: list[dict], *, tree: bool, unmanaged: list[dict] | None = None) -> str:
    if not rows and not unmanaged:
        return "no participants"
    body = flatten_tree(rows, _row_line) if tree else [_row_line(r) for r in rows]
    header = f"{'ID':<14}{'T':<2}   {'HARNESS':<11} {'STATUS':<15} {'PANE':<6} DIRECTORY"
    lines = [header, *body]
    if unmanaged:
        lines.append("")
        lines.append("unmanaged (harness panes not yet adopted):")
        for u in unmanaged:
            cmd = clip_harness(u.get("command"))
            pane = u.get("pane") or "-"
            icon = harness_icon(u.get("harness") or u.get("command"))
            lines.append(
                # The 2-space indent eats into the id column so the tier and
                # harness columns still line up with the participants above.
                f"  {'-':<12}{'?':<2} {icon} {cmd:<11} "
                f"{'-':<15} {pane:<6} {tilde(u.get('cwd'))}"
            )
    lines.extend(["", TIER_LEGEND])
    return "\n".join(lines)


async def _watch_ls(args, method: str, params: dict) -> int:
    async with DaemonClient() as client:
        while True:
            rows = await client.call(method, **params)
            assert isinstance(rows, list)
            unmanaged = []
            if not args.tree:
                unmanaged = await client.call("participants.unmanaged")
                assert isinstance(unmanaged, list)
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
            # Checked here rather than by argparse `choices`, because declared
            # harnesses are not in the registry when the parser is built.
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
            f"theater.favourite is {favourite!r}, which is not a known harness "
            f"({known})"
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
            rows = await client.call(
                "bus.tail", limit=_FOLLOW_BATCH, after_id=cursor
            )
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
    print(f"{'':<2} {'NAME':<10} {'SOURCE':<8} {'BINARY':<10} {'INSTALLED':<10} PATH")
    for r in rows:
        # A plugin that would not load has no binary to look for, so
        # "installed: no" would blame PATH for a syntax error. Say broken.
        mark = "broken" if r.get("error") else ("yes" if r["installed"] else "no")
        print(
            f"{r['icon']:<2} {r['name']:<10} {r.get('source', '-'):<8} "
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
        held = (
            paths.socket_path()
            if paths.socket_path().exists()
            else paths.pidfile_path()
        )
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
    "ls": cmd_ls,
    "bus": cmd_bus,
    "spawn": cmd_spawn,
    "kill": cmd_kill,
    "adopt": cmd_adopt,
    "harnesses": cmd_harnesses,
    "stats": cmd_stats,
    "config": cmd_config,
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
