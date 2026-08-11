"""Command line entry point.

Three audiences, one binary:

    theater daemon      the singleton, usually started implicitly by a client
    theater mcp         the per-agent stdio MCP server, started by a harness
    theater ls|spawn    a human at a terminal
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time

from theater import paths
from theater.client import DaemonClient, call_sync
from theater.harness import APPROVALS, HARNESSES
from theater.protocol import RemoteError
from theater.tmux import client as tmux

_TIER_MARK = {"spawned": "S", "adopted": "A", "external": "E"}

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
    spawn.add_argument("harness", choices=sorted(HARNESSES))
    spawn.add_argument("prompt", nargs="?", default="")
    spawn.add_argument(
        "--approval",
        choices=APPROVALS,
        required=True,
        help="Tool approval policy for the new agent. No default, on purpose.",
    )
    spawn.add_argument("--cwd", default=None)
    spawn.add_argument("--parent", dest="parent_id", default=None)
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
        help="Override harness detection. By default the pane's current command is matched against known harness binaries.",
    )
    adopt.add_argument("--json", action="store_true")

    sub.add_parser("regie", help="Launch the régie TUI (run inside tmux).")

    sub.add_parser("stop", help="Shut the daemon down.")

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


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home) :] if path.startswith(home) else path


def _row_line(p: dict, indent: int = 0) -> str:
    mark = _TIER_MARK.get(p["tier"], "?")
    reach = " " if p["addressable"] else "*"
    pad = "  " * indent
    # Clipped, not just padded: a participant is free to report any harness
    # name it likes, and one long one must not shear every column after it.
    harness = (p.get("harness") or "-")[:11]
    return (
        f"{p['id']:<14}{mark}{reach} {harness:<11} "
        f"{p['status']:<15} {p.get('tmux_pane') or '-':<6} {pad}{_tilde(p.get('cwd') or '-')}"
    )


def _tree_lines(nodes: list[dict], indent: int = 0) -> list[str]:
    out: list[str] = []
    for node in nodes:
        out.append(_row_line(node, indent))
        out += _tree_lines(node.get("children", []), indent + 1)
    return out


def _format_ls(rows: list[dict], *, tree: bool, unmanaged: list[dict] | None = None) -> str:
    if not rows and not unmanaged:
        return "no participants"
    body = _tree_lines(rows) if tree else [_row_line(r) for r in rows]
    header = f"{'ID':<14}{'T':<2} {'HARNESS':<11} {'STATUS':<15} {'PANE':<6} DIRECTORY"
    legend = "T: S spawned  A adopted  E external   * not addressable"
    lines = [header, *body]
    if unmanaged:
        lines.append("")
        lines.append("unmanaged (harness panes not yet adopted):")
        for u in unmanaged:
            cmd = (u.get("command") or "-")[:11]
            pane = u.get("pane") or "-"
            lines.append(f"  {'-':<14}{'?'}  {cmd:<11} {'-':<15} {pane:<6} {_tilde(u.get('cwd') or '-')}")
    lines.extend(["", legend])
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


def cmd_spawn(args) -> int:
    record = call_sync(
        "spawn",
        harness=args.harness,
        prompt=args.prompt,
        approval=args.approval,
        cwd=args.cwd or os.getcwd(),
        parent_id=args.parent_id,
        tmux_session=tmux.current_session_sync(),
        background=not args.foreground,
    )
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        assert isinstance(record, dict)
        print(f"{record['id']}  {record['harness']}  pane {record['tmux_pane']}")
    return 0


def _summary(payload: dict | None) -> str:
    """One line describing an event, whatever kind it is.

    The bus carries both agent activity and registry bookkeeping, so this
    prefers the fields the observer writes and falls back to raw JSON rather
    than dropping information it does not recognise.
    """
    if not payload:
        return ""
    bits = []
    if payload.get("tool"):
        bits.append(f"[{payload['tool']}]")
    if payload.get("text"):
        bits.append(" ".join(str(payload["text"]).split()))
    if not bits:
        known = {"ts", "turn_end", "index"}
        rest = {k: v for k, v in payload.items() if k not in known and v is not None}
        if rest:
            bits.append(json.dumps(rest, separators=(",", ":")))
    if payload.get("turn_end"):
        bits.append("(turn end)")
    return " ".join(bits)


def _bus_line(row: dict, width: int) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(row.get("ts") or 0))
    who = row.get("from_id") or "-"
    if row.get("to_id"):
        who = f"{who} -> {row['to_id']}"
    line = f"{stamp}  {row.get('kind', '?'):<18} {who:<32} {_summary(row.get('payload'))}"
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
    record = call_sync("adopt", pane=pane, harness=args.harness, cwd=os.getcwd())
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        assert isinstance(record, dict)
        mark = _TIER_MARK.get(record["tier"], "?")
        print(f"{record['id']}  {mark} {record['harness']}  pane {record['tmux_pane']}")
    return 0


def cmd_stop(args) -> int:
    call_sync("shutdown")
    print("daemon stopping")
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
    from theater.regie.app import run_regie

    run_regie()
    return 0


_COMMANDS = {
    "daemon": cmd_daemon,
    "mcp": cmd_mcp,
    "ls": cmd_ls,
    "bus": cmd_bus,
    "spawn": cmd_spawn,
    "kill": cmd_kill,
    "adopt": cmd_adopt,
    "regie": cmd_regie,
    "stop": cmd_stop,
}


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths.ensure_home()
    try:
        return _COMMANDS[args.command](args)
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
