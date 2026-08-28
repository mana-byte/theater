"""Resume reference resolution, safety/identity validation, and floor capture.

All resume logic runs *before* a participant or worktree is created, so
a refused resume leaves nothing behind.
"""

from __future__ import annotations

from collections.abc import Sequence

from theater.daemon.registry import Registry
from theater.daemon.spawning.models import SpawnRequest
from theater.harness import normalize as normalize_harness
from theater.harness.base import ResumeLaunchOverlay
from theater.models import BadRequest, Participant, Status
from theater.provenance import is_trusted_provenance
from theater.resume_floor import UNKNOWN_FLOOR, encode_floor

__all__ = [
    "capture_resume_floor",
    "reject_unsafe_resume_shape",
    "resolve_resume_reference",
    "validate_before_create",
    "validate_resume_identity",
]


def resolve_resume_reference(req: SpawnRequest, registry: Registry) -> SpawnRequest:
    """If ``resume`` is a Theater participant id, resolve it to the harness
    session id the daemon already holds.

    Participant primary-key matches take precedence: if the value is an
    exact row id, it is resolved here. Otherwise the value is treated as a
    native harness session id and the existing path handles it.
    """
    from dataclasses import replace

    if req.resume is None:
        return req
    participant = registry.store.get_participant(req.resume)
    if participant is None:
        return req
    if normalize_harness(participant.harness) != normalize_harness(req.harness):
        raise BadRequest(
            f"participant {participant.id!r} belongs to harness "
            f"{participant.harness!r}, not {req.harness!r}"
        )
    if participant.status is not Status.DEAD:
        raise BadRequest(f"cannot resume participant {participant.id!r}: it is still live")
    if not participant.session_id:
        raise BadRequest(
            f"cannot resume participant {participant.id!r}: "
            "Theater has not recorded its harness session id"
        )
    return replace(req, resume=participant.session_id)


def reject_unsafe_resume_shape(req: SpawnRequest, harness) -> None:
    """Refuse resume combinations that are unsafe or silently dropped."""
    if req.resume and req.prompt and not harness.resume_takes_prompt:
        raise BadRequest(
            f"harness {req.harness!r} cannot resume a session with a prompt; "
            f"resume it without one and use send to deliver the task"
        )
    if req.resume and req.response_format and not harness.resume_takes_prompt:
        raise BadRequest(
            f"harness {req.harness!r} cannot resume a session with response_format; "
            f"resume it without one and use send to deliver the task"
        )
    if req.resume and req.worktree:
        raise BadRequest(
            "cannot resume into a worktree: the session's transcript "
            "describes files that are not the worktree's files"
        )


def validate_before_create(
    req: SpawnRequest, harness, registry: Registry
) -> tuple[Participant | None, ResumeLaunchOverlay | None]:
    """Refuse unsafe launches before a participant or worktree exists."""
    from theater.harness import check_model, check_reasoning, check_resume

    check_model(req.harness, req.model)
    check_reasoning(req.harness, req.reasoning_effort)
    check_resume(req.harness, req.resume)
    reject_unsafe_resume_shape(req, harness)
    predecessor, trusted_owners = validate_resume_identity(req, registry)
    overlay: ResumeLaunchOverlay | None = None
    if predecessor is not None:
        harness.resume_preflight(predecessor=predecessor)
        overlay = harness.resume_launch_overlay(
            predecessor=predecessor,
            trusted_session_owners=trusted_owners,
        )
    return predecessor, overlay


def validate_resume_identity(
    req: SpawnRequest, registry: Registry
) -> tuple[Participant | None, Sequence[Participant]]:
    """Resume only daemon-validated trusted session ids.

    Returns ``(predecessor, trusted_owners)``: the selected newest dead
    predecessor, and the complete trusted matching set.
    """
    if req.resume is None:
        return None, ()
    canonical = normalize_harness(req.harness)
    live_matches = []
    dead_matches = []
    for participant in registry.list(include_dead=True):
        if (
            normalize_harness(participant.harness) == canonical
            and participant.session_id == req.resume
            and is_trusted_provenance(participant.session_correlation)
        ):
            if participant.status is Status.DEAD:
                dead_matches.append(participant)
            else:
                live_matches.append(participant)
    if live_matches:
        live = max(live_matches, key=lambda p: (p.last_activity, p.created_at))
        raise BadRequest(
            f"cannot resume session {req.resume!r}: trusted owner {live.id} is still "
            "live. Use send to deliver work to the live participant, or wait for it "
            "to die before resuming the session."
        )
    if dead_matches:
        predecessor = max(dead_matches, key=lambda p: (p.last_activity, p.created_at))
        return predecessor, dead_matches
    raise BadRequest(
        f"cannot resume session {req.resume!r}: Theater has no trusted "
        "dead operator/proven/exact binding for that session id. If its owner row "
        "was garbage-collected, resume the session outside Theater, then adopt and "
        "bind that pane, or wait for tombstone support."
    )


def capture_resume_floor(harness, predecessor: Participant) -> str:
    """Capture the predecessor's transcript stream position before launch.

    Returns the encoded floor string. An unreadable or non-file transcript
    produces ``"unknown"``: present-but-unknown is still a floor.
    """
    location = predecessor.transcript_location
    if location is None:
        return UNKNOWN_FLOOR
    point = harness.observer.stream_floor(location)
    return encode_floor(point)
