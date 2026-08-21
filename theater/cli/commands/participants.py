"""Participant commands: ls/watch, spawn, kill, name, adopt."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from theater import config, paths
from theater.cli.errors import BadUsage
from theater.cli.render import _format_ls
from theater.client import DaemonClient, call_sync
from theater.constants.cli import CLI_CLEAR_SCREEN as _CLEAR
from theater.formatting import tier_mark
from theater.harness import HARNESSES
from theater.tmux import client as tmux


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


def cmd_kill(args) -> int:
    call_sync("participant.kill", id=args.id)
    print(f"killed {args.id}")
    return 0


def cmd_name(args) -> int:
    record = call_sync("participant.rename", id=args.target, name=args.new_name)
    assert isinstance(record, dict)
    print(f"renamed {args.target} -> {record.get('name')}")
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
