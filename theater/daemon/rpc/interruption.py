"""Manifest-driven parent-to-child interruption RPC."""

from __future__ import annotations

from theater.constants.daemon import BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED
from theater.daemon.rpc.params import _string_param
from theater.daemon.rpc.router import method
from theater.harness.contracts.runtime import RuntimeCapability


def active_job_handle(daemon, target_id: str) -> str | None:
    """The newest running job actually on the target's turn; queued followups are not."""
    queued = {
        operation.job_handle for operation in daemon.store.queued_control_operations(target_id)
    }
    for job in daemon.store.running_jobs_for_target(target_id):
        if job.handle not in queued:
            return job.handle
    return None


@method("participant.interrupt")
async def _interrupt(daemon, params: dict) -> dict:
    target = daemon.registry.resolve(
        _string_param(params, "target", method_name="participant.interrupt")
    )
    caller_id = _string_param(params, "caller_id", method_name="participant.interrupt")
    target_id = target.id

    route = daemon.controls.route_for(target_id, RuntimeCapability.INTERRUPT)
    del route
    active = active_job_handle(daemon, target_id)
    outcome = await daemon.controls.interrupt(target_id, caller_id=caller_id)
    result = {"id": target_id, "interrupted": outcome.interrupted}
    if outcome.interrupted and active is not None:
        result["interrupted_job"] = active
    if outcome.reason is not None:
        result["reason"] = outcome.reason
    if outcome.cancelled_followups:
        result["cancelled_followups"] = list(outcome.cancelled_followups)
    if outcome.interrupted:
        daemon.store.bus_append(
            BUS_KIND_PARTICIPANT_INTERRUPT_REQUESTED,
            from_id=caller_id,
            to_id=target_id,
        )
    return result
