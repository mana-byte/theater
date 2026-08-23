"""Bounded trajectory wire responses and opaque follow cursor helpers."""

from __future__ import annotations

from collections.abc import Callable

from theater import protocol
from theater.constants.trajectory import (
    TRAJECTORY_RESPONSE_MAX_BYTES,
    TRAJECTORY_RESPONSE_SIZING_REQUEST_ID,
)
from theater.daemon.trajectory.cache import RecordChange
from theater.daemon.trajectory.merge import groups_for_records
from theater.daemon.trajectory.runtime import TrajectoryStream, participant_state
from theater.trajectory import (
    CoverageGap,
    PanelState,
    PanelStateInfo,
    TrajectoryCoverage,
    TrajectoryDelta,
    TrajectoryPage,
    TrajectoryParticipantState,
    TrajectoryRecord,
    TrajectoryUpsert,
)

_OLDER_CURSOR_PLACEHOLDER = "o1-" + "0" * 32


def follow_cursor(daemon_epoch: str, stream: TrajectoryStream, sequence: int) -> str:
    return f"c1-{daemon_epoch}-{stream.cache.stream_id}-{sequence}"


def decode_follow_cursor(value: str) -> tuple[str, str, int] | None:
    parts = value.split("-")
    if len(parts) != 4 or parts[0] != "c1":
        return None
    if not parts[1] or not parts[2] or not parts[3].isdigit():
        return None
    return parts[1], parts[2], int(parts[3])


def fit_page(
    stream: TrajectoryStream,
    records: tuple[TrajectoryRecord, ...],
    *,
    daemon_epoch: str,
    has_older: bool,
    source_before: str | None,
    bus_before: int | None,
    make_older: Callable[[str | None, int | None, str | None], str],
) -> TrajectoryPage:
    """Return the largest suffix whose complete encoded page fits the wire cap."""

    def build(
        count: int,
        older_cursor: str | None,
        coverage: TrajectoryCoverage,
    ) -> TrajectoryPage:
        selected = records[-count:] if count else ()
        byte_truncated = count < len(records)
        older = has_older or byte_truncated
        return TrajectoryPage(
            panel_state=stream.panel_state,
            stream_id=stream.cache.stream_id,
            cursor=follow_cursor(daemon_epoch, stream, stream.ring.current_sequence),
            records=selected,
            groups=groups_for_records(selected),
            older_cursor=older_cursor if older else None,
            has_older=older,
            coverage=coverage,
            truncated_by_bytes=byte_truncated,
        )

    coverage = _bounded_coverage(
        stream,
        lambda candidate: wire_bytes(build(0, _OLDER_CURSOR_PLACEHOLDER, candidate).to_wire()),
    )

    low = 0
    high = len(records)
    best = -1
    while low <= high:
        count = (low + high) // 2
        older = has_older or count < len(records)
        page = build(count, _OLDER_CURSOR_PLACEHOLDER if older else None, coverage)
        if wire_bytes(page.to_wire()) <= TRAJECTORY_RESPONSE_MAX_BYTES:
            best = count
            low = count + 1
        else:
            high = count - 1
    if best < 0:
        raise ValueError("trajectory response envelope exceeds the wire limit")
    byte_truncated = best < len(records)
    older = has_older or byte_truncated
    marker = records[-best].record_id if best else None
    cursor = make_older(source_before, bus_before, marker) if older else None
    return build(best, cursor, coverage)


def fit_delta(
    stream: TrajectoryStream,
    changes: tuple[RecordChange, ...],
    *,
    daemon_epoch: str,
    after_sequence: int,
) -> TrajectoryDelta | None:
    """Return the largest prefix that fits, or None when one update cannot fit."""

    def build(count: int) -> TrajectoryDelta:
        selected = changes[:count]
        sequence = selected[-1].sequence if selected else after_sequence
        return TrajectoryDelta(
            stream_id=stream.cache.stream_id,
            cursor=follow_cursor(daemon_epoch, stream, sequence),
            upserts=tuple(TrajectoryUpsert(change.record) for change in selected),
        )

    low = 1
    high = len(changes)
    best: TrajectoryDelta | None = None
    while low <= high:
        count = (low + high) // 2
        delta = build(count)
        if wire_bytes(delta.to_wire()) <= TRAJECTORY_RESPONSE_MAX_BYTES:
            best = delta
            low = count + 1
        else:
            high = count - 1
    return best


def empty_delta(stream: TrajectoryStream, *, daemon_epoch: str, sequence: int) -> TrajectoryDelta:
    return TrajectoryDelta(
        stream_id=stream.cache.stream_id,
        cursor=follow_cursor(
            daemon_epoch,
            stream,
            max(sequence, stream.ring.current_sequence),
        ),
    )


def resync_delta(stream_id: str, reason: str) -> TrajectoryDelta:
    return TrajectoryDelta(stream_id=stream_id, resync_required=True, reason=reason)


def stale_page(stream: TrajectoryStream, *, daemon_epoch: str, message: str) -> TrajectoryPage:
    def build(coverage: TrajectoryCoverage) -> TrajectoryPage:
        return TrajectoryPage(
            panel_state=PanelStateInfo(
                PanelState.STALE,
                message,
                participant_state(stream.participant),
            ),
            stream_id=stream.cache.stream_id,
            cursor=follow_cursor(daemon_epoch, stream, stream.ring.current_sequence),
            coverage=coverage,
        )

    coverage = _bounded_coverage(stream, lambda candidate: wire_bytes(build(candidate).to_wire()))
    return build(coverage)


def missing_page() -> TrajectoryPage:
    return TrajectoryPage(
        panel_state=PanelStateInfo(
            PanelState.UNAVAILABLE,
            "participant is missing; refresh the participant tree and select an existing id",
            TrajectoryParticipantState.MISSING,
        )
    )


def wire_bytes(value: dict[str, object]) -> int:
    return len(protocol.ok(TRAJECTORY_RESPONSE_SIZING_REQUEST_ID, value))


def _bounded_coverage(
    stream: TrajectoryStream,
    fits: Callable[[TrajectoryCoverage], int],
) -> TrajectoryCoverage:
    """Return recent coverage gaps whose complete response stays within the cap."""
    gaps = tuple(stream.gaps)

    def build(retained: tuple[CoverageGap, ...]) -> TrajectoryCoverage:
        omitted = len(gaps) - len(retained)
        marker = (
            (CoverageGap("coverage", f"{omitted} older coverage gaps omitted"),) if omitted else ()
        )
        return TrajectoryCoverage(
            transcript_floor=stream.transcript_floor,
            theater_floor=stream.theater_floor,
            gaps=(*marker, *retained),
        )

    retained: tuple[CoverageGap, ...] = ()
    for gap in reversed(gaps):
        candidate = (gap, *retained)
        if fits(build(candidate)) > TRAJECTORY_RESPONSE_MAX_BYTES:
            break
        retained = candidate
    coverage = build(retained)
    if fits(coverage) > TRAJECTORY_RESPONSE_MAX_BYTES:
        raise ValueError("trajectory coverage exceeds the wire limit")
    return coverage


__all__ = [
    "decode_follow_cursor",
    "empty_delta",
    "fit_delta",
    "fit_page",
    "follow_cursor",
    "missing_page",
    "resync_delta",
    "stale_page",
    "wire_bytes",
]
