"""Private recall RPC adapters and compatibility projection."""

from __future__ import annotations

from theater.daemon import recall_operations
from theater.daemon.rpc.router import method

_attach_parent_names = recall_operations._attach_parent_names


@method("recall")
async def _recall(daemon, params: dict) -> dict:
    return await recall_operations.recall(daemon, params)


@method("recall_read")
async def _recall_read(daemon, params: dict) -> dict:
    return await recall_operations.recall_read(daemon, params)
