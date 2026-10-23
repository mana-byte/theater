"""Private RPC adapter for participant interruption."""

from theater.daemon.controls.interruption_request import active_job_handle, interrupt
from theater.daemon.rpc.router import method


@method("participant.interrupt")
async def _interrupt(daemon, params: dict) -> dict:
    return await interrupt(daemon, params)


__all__ = ["_interrupt", "active_job_handle"]
