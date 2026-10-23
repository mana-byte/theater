"""Private spawn and catalog RPC adapters."""

from __future__ import annotations

from theater.daemon import catalogs
from theater.daemon.rpc.router import method
from theater.daemon.spawning import private_operations

_PrivateSpawnMapping = private_operations._PrivateSpawnMapping
_wiring_param = private_operations._wiring_param


@method("spawn")
async def _spawn(daemon, params: dict) -> dict:
    return await private_operations.spawn(daemon, params)


@method("harnesses")
async def _harnesses(daemon, params: dict) -> list[dict]:
    return await catalogs.harnesses(daemon)


@method("models")
async def _models(daemon, params: dict) -> list[dict]:
    return catalogs.models(daemon)
