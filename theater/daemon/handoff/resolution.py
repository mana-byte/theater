"""Resolve ``after`` once, to one stable source job."""

from __future__ import annotations

from theater.models import BadRequest, Job, NotFound, Participant


def resolve_source(daemon, token: object) -> tuple[Job, Participant | None]:
    """An exact job handle, or a live participant name (or id) meaning its newest tasked job.

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
        job = daemon.store.newest_turn_job(participant.id)
        if job is None:
            raise BadRequest(
                f"participant {participant.id!r} has no tasked job to hand off from; send it "
                "work first or pass an exact job handle"
            )
    return job, participant_of(daemon, job.target_id)


def participant_of(daemon, participant_id: str | None) -> Participant | None:
    """The participant with its live name applied, or None once the row is gone."""
    if participant_id is None:
        return None
    try:
        return daemon.registry.get(participant_id)
    except NotFound:
        return None
