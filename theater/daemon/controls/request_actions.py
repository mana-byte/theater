"""Private and plugin steer/queue requests over the control service."""

from __future__ import annotations

from theater.constants.refusals import NEXT_STEER_STALE
from theater.daemon.request_values import optional_string_param, string_param
from theater.daemon.response_contracts import resolve_response_format
from theater.daemon.skill_attachments import attach_skills
from theater.daemon.skill_cards import compose_prompt
from theater.harness.contracts.runtime import ControlKind, DeliveryResult
from theater.models import StaleTarget


def steer_receipt(daemon, job) -> dict:
    """The flat additive delivery facts for the steer that just finished."""
    steer_operations = [
        operation
        for operation in daemon.store.control_operations_for_job(job.handle)
        if operation.kind is ControlKind.STEER
    ]
    latest = (
        max(steer_operations, key=lambda item: (item.updated_at, item.created_at))
        if steer_operations
        else None
    )
    if latest is None:
        return {
            "delivery": "unknown",
            "phase": None,
            "operation_id": None,
            "reason": "steer_operation_missing",
            "detail": (
                f"the persisted STEER operation for job {job.handle!r} could not "
                "be read back after the service returned; the amendment's "
                "delivery is unknown — do not retry blindly"
            ),
        }
    receipt: dict = {
        "delivery": "accepted" if latest.delivery_result is DeliveryResult.ACCEPTED else "unknown",
        "phase": str(latest.delivery_phase),
        "operation_id": latest.operation_id,
    }
    if latest.error_code is not None:
        receipt["reason"] = latest.error_code
    if latest.error is not None:
        receipt["detail"] = latest.error
    return receipt


async def steer(daemon, params: dict) -> dict:
    """Amend the current turn and report its persisted delivery receipt."""
    method_name = "participant.steer"
    target = daemon.registry.resolve(string_param(params, "target", method_name=method_name))
    prompt = string_param(params, "prompt", method_name=method_name)
    caller_id = string_param(params, "caller_id", method_name=method_name)
    job_handle = optional_string_param(params, "job_handle", method_name=method_name)
    try:
        job = await daemon.controls.steer(
            target.id, caller_id=caller_id, prompt=prompt, job_handle=job_handle
        )
    except StaleTarget as exc:
        exc.details = exc.details or {"next": NEXT_STEER_STALE}
        raise
    result = job.to_dict()
    result.update(steer_receipt(daemon, job))
    daemon.store.bus_append(
        "agent.steer",
        from_id=caller_id,
        to_id=target.id,
        payload={"handle": job.handle, "prompt": prompt[:200], "delivery": result["delivery"]},
    )
    return result


async def queue_followup(daemon, params: dict) -> dict:
    """Reserve a followup with its guidance and skill cards injected once."""
    method_name = "participant.queue_followup"
    target = daemon.registry.resolve(string_param(params, "target", method_name=method_name))
    prompt = string_param(params, "prompt", method_name=method_name)
    caller_id = string_param(params, "caller_id", method_name=method_name)
    response_format = resolve_response_format(
        daemon.store, params.get("response_format"), creator_id=caller_id
    )
    attachment = attach_skills(daemon, params, task=prompt, sender=caller_id)
    assignment = daemon.plays.check_send(caller_id, target.id, attachment.plan)
    job = await daemon.controls.queue_followup(
        target.id,
        caller_id=caller_id,
        prompt=compose_prompt(prompt, skills=attachment.block, response_format=response_format),
        response_format=response_format,
        on_reserved=daemon.plays.holder(assignment),
    )
    daemon.store.bus_append(
        "agent.queue_followup",
        from_id=caller_id,
        to_id=target.id,
        payload={"handle": job.handle, "prompt": prompt[:200]},
    )
    return job.to_dict()
