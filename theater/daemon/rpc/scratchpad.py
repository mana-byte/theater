"""Private RPC adapters for bounded scratchpad operations."""

from __future__ import annotations

from theater.daemon import scratchpad_operations
from theater.daemon.rpc.router import method


@method("scratchpad.write")
async def _scratchpad_write(daemon, params: dict) -> dict:
    return await scratchpad_operations.write(daemon, params)


@method("scratchpad.get")
async def _scratchpad_get(daemon, params: dict) -> dict:
    return await scratchpad_operations.get(daemon, params)


@method("scratchpad.delete")
async def _scratchpad_delete(daemon, params: dict) -> dict:
    return await scratchpad_operations.delete(daemon, params)
