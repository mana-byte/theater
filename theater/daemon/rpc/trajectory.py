"""Trajectory snapshot, follow, and best-effort viewer-release RPCs."""

from __future__ import annotations

from theater.daemon.rpc.router import method
from theater.daemon.trajectory import operations


@method("trajectory.snapshot")
async def _trajectory_snapshot(daemon, params: dict) -> dict:
    return (await operations.snapshot(daemon, params)).to_wire()


@method("trajectory.follow")
async def _trajectory_follow(daemon, params: dict) -> dict:
    return (await operations.follow(daemon, params)).to_wire()


@method("trajectory.close")
async def _trajectory_close(daemon, params: dict) -> dict:
    return {"released": operations.close(daemon, params)}


@method("trajectory.locate")
async def _trajectory_locate(daemon, params: dict) -> dict:
    return operations.locate(daemon, params).to_wire()


@method("trajectory.search")
async def _trajectory_search(daemon, params: dict) -> dict:
    return (await operations.search(daemon, params)).to_wire()


__all__ = []
