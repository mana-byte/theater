"""Identity-fenced, bounded agent transcript reads."""

from __future__ import annotations

from theater.constants.daemon import TRANSCRIPT_READ_RESPONSE_MAX_BYTES, TRANSCRIPT_READABLE_KINDS
from theater.daemon.observation.process import observation_process_id
from theater.daemon.request_values import optional_string_param, require
from theater.daemon.transcript_context import newest_page_context
from theater.daemon.transcript_paging import (
    TRANSCRIPT_COLLAPSE_MIN_BYTES,
    EventProjection,
    TranscriptCursorError,
    TranscriptPager,
)
from theater.harness import HARNESSES, normalize
from theater.harness.contracts.source import History
from theater.harness.transcript.observer import open_participant_source
from theater.models import BadRequest, Status, Tier, TranscriptIdentityLost
from theater.provenance import is_trusted_provenance, normalize_provenance
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)

READABLE = TRANSCRIPT_READABLE_KINDS


def _transcript_identity_lost(daemon, pid: str) -> bool:
    checker = getattr(daemon.observer, "transcript_identity_lost", None)
    return bool(checker(pid)) if callable(checker) else False


async def read_transcript_page(
    daemon,
    *,
    participant_id: str,
    cursor: str | None = None,
    max_bytes: int = TRANSCRIPT_READ_RESPONSE_MAX_BYTES,
    event_projection: EventProjection | None = None,
    roles: tuple[str, ...] | None = None,
    collapse_over: int | None = None,
    include_answer_seals: bool = False,
) -> dict:
    """Read one bounded, reverse-paginated transcript page.

    Via a short-lived ``Source`` (works for database-backed adapters too), separate from
    the watcher's so reading history never moves its cursor.
    """
    p = daemon.registry.get(participant_id)
    pid = p.id
    if type(max_bytes) is not int or max_bytes <= 0:
        raise BadRequest("read_transcript max_bytes must be a positive integer")

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot read transcript: harness {p.harness!r} is not known")
    if _transcript_identity_lost(daemon, pid):
        raise TranscriptIdentityLost(transcript_identity_recovery_message(pid))

    # Same birth-time floor as the watch path: applies to SPAWNED participants only.
    after = p.created_at if p.tier is Tier.SPAWNED else None
    source = open_participant_source(
        harness.observer,
        participant_id=p.id,
        cwd=p.cwd,
        session_id=p.session_id,
        after=after,
        session_provenance=normalize_provenance(p.session_correlation),
        known_location=p.transcript_location,
        transcript_domain=p.transcript_domain,
        pane_pid=observation_process_id(daemon.registry.store, p),
    )
    try:
        try:
            page = await TranscriptPager(
                source,
                target=pid,
                event_filter=lambda event: event.kind.value in READABLE,
                max_bytes=max_bytes,
                event_projection=event_projection,
                roles=roles,
                collapse_over=collapse_over,
            ).read(cursor)
        except TranscriptCursorError as exc:
            raise BadRequest(str(exc)) from None
    finally:
        await source.aclose()

    history_page = page.history
    history = History(
        location=history_page.location,
        correlation=history_page.provenance,
        collision_domain=history_page.collision_domain,
        pinned=history_page.pinned,
    )
    if history_page.error_code is not None:
        if history_page.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
            if p.status is Status.DEAD:
                raise BadRequest(
                    "cannot read transcript: trusted dead binding is retained for resume, "
                    "but its transcript is unavailable"
                )
            raise TranscriptIdentityLost(
                transcript_identity_recovery_message(pid, history_page.error)
            )
        detail = f"cannot read transcript: {history_page.error or history_page.error_code} "
        detail += f"({history_page.error_code})"
        if cursor is not None and history_page.error_code == "history_cursor_invalid":
            detail += "; omit cursor to restart from newest"
        raise BadRequest(detail)
    if history_page.location is None:
        raise BadRequest("cannot read transcript: transcript no longer exists on disk")
    if not is_trusted_provenance(history_page.provenance):
        raise BadRequest(
            "cannot read transcript: this session is known only from cwd/time; "
            "wait for exact/proven correlation or bind the session before reading it "
            "(transcript_correlation_untrusted)"
        )
    if daemon.observer.history_is_ambiguous(pid, history):
        raise BadRequest(
            "cannot read transcript: its session is known only from cwd/time and another "
            "live participant of the same harness shares that transcript root and cwd "
            "(transcript_correlation_ambiguous)"
        )
    return page.to_wire(
        target=pid, event_projection=event_projection, include_answer_seals=include_answer_seals
    )


async def read_transcript(daemon, params: dict) -> dict:
    if "last_n" in params:
        raise BadRequest(
            "read_transcript does not accept event counts; use the next_cursor returned by Theater"
        )
    target = daemon.registry.resolve(require(params, "id"))
    cursor = optional_string_param(params, "cursor", method_name="read_transcript")
    roles = roles_param(params.get("roles"))
    page = await read_transcript_page(
        daemon,
        participant_id=target.id,
        cursor=cursor,
        roles=roles,
        collapse_over=TRANSCRIPT_COLLAPSE_MIN_BYTES,
        include_answer_seals=True,
    )
    if cursor is None:
        page.update(newest_page_context(daemon.registry.store, target))
    return page


def roles_param(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(role not in READABLE for role in value):
        raise BadRequest(
            f"read_transcript roles must be a non-empty list drawn from {list(READABLE)}; "
            "omit roles to read every kind"
        )
    return tuple(role for role in READABLE if role in value)
