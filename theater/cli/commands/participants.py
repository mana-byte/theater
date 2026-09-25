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
from theater.harness import HARNESSES


async def _watch_ls(args, method: str, params: dict) -> int:
    async with DaemonClient() as client:
        while True:
            rows = await client.call(method, **params)
            assert isinstance(rows, list)
            stamp = time.strftime("%H:%M:%S")
            frame = _format_ls(rows, tree=args.tree)
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
    if args.json:
        print(json.dumps({"participants": rows}, indent=2))
        return 0
    print(_format_ls(rows, tree=args.tree))
    return 0


def _spawn_harness(args) -> str:
    """The harness to spawn: the one named, else the configured favourite.

    An unset favourite is an actionable error, never a silent guess.
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
        provider=args.provider,
        parent_id=args.parent_id,
        background=not args.foreground,
        worktree=args.worktree,
        base_branch=args.base_branch,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        wiring=args.wiring,
    )
    if args.json:
        print(json.dumps(record, indent=2))
    else:
        assert isinstance(record, dict)
        if "operation_id" in record:
            print(
                f"{record['id']}  {record['harness']}  accepted operation {record['operation_id']}"
            )
        else:
            print(f"{record['id']}  {record['harness']}  accepted")
    return 0


def cmd_kill(args) -> int:
    result = call_sync("participant.kill", id=args.id)
    assert isinstance(result, dict)
    print(f"killed {args.id}" if result.get("killed") else f"already dead: {args.id}")
    cleanup = result.get("workspace_cleanup")
    if isinstance(cleanup, dict):
        print(f"workspace {cleanup['workspace_id']}: cleanup {cleanup['state']}")
        if cleanup.get("state") != "succeeded":
            print(f"inspect with: theater workspaces get {cleanup['workspace_id']}")
    return 0


def cmd_name(args) -> int:
    record = call_sync("participant.rename", id=args.target, name=args.new_name)
    assert isinstance(record, dict)
    print(f"renamed {args.target} -> {record.get('name')}")
    return 0


def cmd_adopt(args) -> int:
    """Reject the retired implicit-pane adoption path with actionable guidance."""
    del args
    raise BadUsage(
        "implicit tmux-pane adoption was removed; use a provider-aware frontend "
        "that supplies the exact provider, generation, terminal incarnation, and occupant"
    )
