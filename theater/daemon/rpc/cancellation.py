"""``jobs.cancel``: a requester withdraws a send that has not been transmitted."""

from __future__ import annotations

from theater.daemon.rpc.params import _string_param
from theater.daemon.rpc.router import method


@method("jobs.cancel")
async def _cancel(daemon, params: dict) -> dict:
    handle = _string_param(params, "handle", method_name="jobs.cancel")
    requester_id = params.get("caller_id") or "cli"
    outcome = await daemon.controls.cancel_job(handle, requester_id=requester_id)
    result: dict = {"handle": outcome.handle, "cancelled": outcome.cancelled}
    if outcome.reason is not None:
        result["reason"] = outcome.reason
    if outcome.state is not None:
        result["state"] = outcome.state
    if outcome.cancelled:
        job = daemon.store.get_job(handle)
        daemon.store.bus_append(
            "agent.cancel_job",
            from_id=requester_id,
            to_id=job.target_id if job else None,
            payload={"handle": handle},
        )
    return result
