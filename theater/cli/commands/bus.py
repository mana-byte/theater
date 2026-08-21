"""Bus tail and follow commands."""

from __future__ import annotations

import asyncio
import json
import sys

from theater.cli.render import _bus_line, _matching, _width
from theater.client import DaemonClient, call_sync
from theater.constants.cli import CLI_FOLLOW_BATCH_SIZE as _FOLLOW_BATCH


def _emit_bus(rows: list[dict], args) -> None:
    width = _width()
    for row in rows:
        print(json.dumps(row) if args.json else _bus_line(row, width))
    # Flush so piped output is not block-buffered into a hang.
    sys.stdout.flush()


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
