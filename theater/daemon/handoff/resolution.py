"""Resolve ``after`` once, to one stable source job."""

from __future__ import annotations

from theater.harness.contracts.runtime import ControlDeliveryPhase, DeliveryResult
from theater.models import BadRequest, Job, JobState, NotFound, Participant

_SCAN_LIMIT = 64


def resolve_source(daemon, token: object) -> tuple[Job, Participant | None]:
    """An exact job handle, or a live participant name (or id) meaning its current tasked job.

    The result is a job handle: a name recycled later cannot redirect a dependency already made.
    """
    if not isinstance(token, str) or not token.strip():
        raise BadRequest("after must be a live participant name or an exact job handle")
    job = daemon.store.get_job(token)
    if job is None:
        try:
            participant = daemon.registry.resolve(token)
        except NotFound:
            raise BadRequest(
                f"after {token!r} is neither a job handle nor a live participant name; names of "
                "dead participants are released, so pass the job handle you were given"
            ) from None
        job = _current_tasked_job(daemon, participant.id)
        if job is None:
            raise BadRequest(
                f"participant {participant.id!r} has no started tasked job to hand off from; "
                "send it work first or pass an exact job handle"
            )
    return job, participant_of(daemon, job.target_id)


def _current_tasked_job(daemon, participant_id: str) -> Job | None:
    """The participant's own work: its running job, else its latest one, never a queued message.

    Another sender's message still waiting in the queue is not this participant's work yet,
    and naming the participant must not silently mean it.
    """
    waiting = {
        operation.job_handle for operation in daemon.store.queued_control_operations(participant_id)
    }
    candidates = [
        job
        for job in daemon.store.recent_turn_jobs(participant_id, limit=_SCAN_LIMIT)
        if job.handle not in waiting and _was_started(daemon, job)
    ]
    return next((job for job in candidates if job.state == JobState.RUNNING), None) or (
        candidates[0] if candidates else None
    )


def _was_started(daemon, job: Job) -> bool:
    """A settled queue rejection is never the participant's work, even after cancellation."""
    if job.kind != "send":
        return True
    operations = daemon.store.control_operations_for_job(job.handle)
    if not operations:  # legacy sends and older jobs may have no control row
        return True
    return any(
        operation.delivery_phase is ControlDeliveryPhase.DISPATCHED
        or operation.delivery_result in (DeliveryResult.ACCEPTED, DeliveryResult.UNKNOWN)
        or operation.native_turn_id is not None
        for operation in operations
    )


def participant_of(daemon, participant_id: str | None) -> Participant | None:
    """The participant with its live name applied, or None once the row is gone."""
    if participant_id is None:
        return None
    try:
        return daemon.registry.get(participant_id)
    except NotFound:
        return None
