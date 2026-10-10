"""Measure what a full scratchpad namespace costs a caller: read bytes and quota-error content.

Fills a temp store (never ~/.theater) through the daemon's private RPC handlers.
"Before" is this same script run against the base commit's checkout (see --help).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from theater.daemon.rpc.scratchpad import _scratchpad_get, _scratchpad_write
from theater.daemon.scratchpad import ScratchpadService
from theater.daemon.store import Store
from theater.models import BadRequest


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


async def _write(daemon, namespace: str, key: str, value: str) -> str | None:
    try:
        await _scratchpad_write(daemon, {"namespace": namespace, "key": key, "value": value})
    except BadRequest as exc:
        return str(exc)
    return None


async def measure(entries: int, value_bytes: int) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "theater.db")
        try:
            service = ScratchpadService(
                store._scratchpad, store.write_unit, ttl_days=7.0, clock=_Clock()
            )
            daemon = SimpleNamespace(store=store, scratchpad_service=service)
            for i in range(entries):
                await _write(daemon, "full", f"note-{i:04d}", "x" * value_bytes)
            entry_error = await _write(daemon, "full", "one-more", "x")
            for i in range(4):
                await _write(daemon, "heavy", f"blob-{i}", "y" * (250 * 1024))
            byte_error = await _write(daemon, "heavy", "blob-4", "y" * (250 * 1024))
            full = await _scratchpad_get(daemon, {"namespace": "full"})
            keys_only = await _scratchpad_get(daemon, {"namespace": "full", "keys_only": True})
        finally:
            store.close()
    return {
        "full_bytes": len(json.dumps(full).encode()),
        "keys_only_bytes": len(json.dumps(keys_only).encode()),
        "keys_only_has_values": isinstance(keys_only["entries"], dict),
        "entry_error": entry_error or "",
        "byte_error": byte_error or "",
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__ + "\nBefore: git worktree add --detach /tmp/base-wpd 4104dca4 && "
        "uv run --project /tmp/base-wpd python scripts/bench/scratchpad_cost.py"
    )
    parser.add_argument("--entries", type=int, default=512)
    parser.add_argument("--value-bytes", type=int, default=1200)
    args = parser.parse_args()
    result = await measure(args.entries, args.value_bytes)
    print(f"theater: {Path(sys.modules['theater'].__file__).parent}")
    print(f"{'metric':<44}{'value':>12}")
    print(f"{'full scratchpad_get bytes':<44}{result['full_bytes']:>12}")
    print(f"{'scratchpad_get keys_only bytes':<44}{result['keys_only_bytes']:>12}")
    print(f"{'keys_only response carries values':<44}{result['keys_only_has_values']!s:>12}")
    for label in ("entry_error", "byte_error"):
        text = result[label]
        print(f"{label + ' chars':<44}{len(text):>12}")
        print(f"{label + ' names oldest keys':<44}{('oldest' in text)!s:>12}")
        print(f"  {text}")


if __name__ == "__main__":
    asyncio.run(main())
