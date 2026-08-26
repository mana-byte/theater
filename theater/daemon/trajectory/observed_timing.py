"""Observation-time fallback for transcript trajectory records."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from theater.constants.daemon import (
    AGENT_OBSERVATION_KINDS,
    BUS_KIND_AGENT_ASSISTANT,
    BUS_KIND_AGENT_ERROR,
    BUS_KIND_AGENT_TOOL_CALL,
    BUS_KIND_AGENT_TOOL_RESULT,
    BUS_KIND_AGENT_TRANSCRIPT,
    BUS_KIND_AGENT_USER,
    BUS_PARTICIPANT_PAGE_MAX_LIMIT,
)
from theater.constants.trajectory import TRAJECTORY_OBSERVATION_TIMING_ROW_LIMIT
from theater.trajectory import (
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)
from theater.transcript_identity import same_location

_KIND_BY_BUS_KIND = {
    BUS_KIND_AGENT_USER: TrajectoryKind.USER,
    BUS_KIND_AGENT_ASSISTANT: TrajectoryKind.ASSISTANT,
    BUS_KIND_AGENT_TOOL_CALL: TrajectoryKind.TOOL_CALL,
    BUS_KIND_AGENT_TOOL_RESULT: TrajectoryKind.TOOL_RESULT,
    BUS_KIND_AGENT_ERROR: TrajectoryKind.ERROR,
}
_TERMINAL_STATUSES = frozenset(
    {
        TrajectoryStatus.COMPLETED,
        TrajectoryStatus.ERROR,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class ObservationPoint:
    row_id: int
    raw_index: int
    kind: TrajectoryKind
    observed_at: float


def observation_points_for_history(
    store,
    participant_id: str,
    location: str | None,
) -> tuple[ObservationPoint, ...]:
    """Load bounded observations after the latest matching transcript attachment."""
    if location is None:
        return ()
    anchors = store.bus_page_for_participant(
        participant_id,
        limit=1,
        kinds={BUS_KIND_AGENT_TRANSCRIPT},
    )
    if not anchors:
        return ()
    anchor = max(anchors, key=lambda row: _row_id(row) or -1)
    anchor_id = _row_id(anchor)
    payload = anchor.get("payload")
    path = payload.get("path") if isinstance(payload, Mapping) else None
    if anchor_id is None or not isinstance(path, str) or not same_location(path, location):
        return ()

    rows: list[Mapping[str, object]] = []
    before_id: int | None = None
    while len(rows) < TRAJECTORY_OBSERVATION_TIMING_ROW_LIMIT:
        page_limit = min(
            BUS_PARTICIPANT_PAGE_MAX_LIMIT,
            TRAJECTORY_OBSERVATION_TIMING_ROW_LIMIT - len(rows),
        )
        page = store.bus_page_for_participant(
            participant_id,
            before_id=before_id,
            limit=page_limit,
            kinds=AGENT_OBSERVATION_KINDS,
        )
        if not page:
            break
        page_ids = [row_id for row in page if (row_id := _row_id(row)) is not None]
        rows.extend(row for row in page if (_row_id(row) or -1) > anchor_id)
        if not page_ids or min(page_ids) <= anchor_id or len(page) < page_limit:
            break
        before_id = min(page_ids)
    return observation_points(rows)


def observation_points(rows: Iterable[Mapping[str, object]]) -> tuple[ObservationPoint, ...]:
    """Decode valid persisted agent observations in deterministic bus order."""
    points: list[ObservationPoint] = []
    for row in sorted(rows, key=lambda value: _row_id(value) or -1):
        row_id = _row_id(row)
        bus_kind = row.get("kind")
        kind = _KIND_BY_BUS_KIND.get(bus_kind) if isinstance(bus_kind, str) else None
        payload = row.get("payload")
        if row_id is None or kind is None or not isinstance(payload, Mapping):
            continue
        raw_index = payload.get("index")
        observed_at = payload.get("observed_at")
        if type(observed_at) not in (int, float):
            observed_at = row.get("ts")
        timestamp = _finite_number(observed_at)
        if type(raw_index) is not int or raw_index < 0 or timestamp is None:
            continue
        points.append(ObservationPoint(row_id, raw_index, kind, timestamp))
    return tuple(points)


def apply_observation_points(
    records: Iterable[TrajectoryRecord],
    points: Iterable[ObservationPoint],
) -> tuple[TrajectoryRecord, ...]:
    """Fill missing timing from observations of the same source record."""
    values = tuple(records)
    observed_by_index: dict[int, float] = {}
    for point in points:
        previous = observed_by_index.get(point.raw_index)
        observed_by_index[point.raw_index] = (
            point.observed_at if previous is None else min(previous, point.observed_at)
        )
    return tuple(
        with_observed_time(record, observed_by_index[record.raw_index])
        if record.raw_index in observed_by_index
        else record
        for record in values
    )


def apply_live_observation(
    records: Iterable[TrajectoryRecord],
    observed_at: float,
    previous: Mapping[str, TrajectoryRecord],
) -> tuple[TrajectoryRecord, ...]:
    """Stamp one captured batch while retaining each record's first observation."""
    return tuple(
        with_observed_time(record, observed_at, previous=previous.get(record.record_id))
        for record in records
    )


def with_observed_time(
    record: TrajectoryRecord,
    observed_at: float,
    *,
    previous: TrajectoryRecord | None = None,
) -> TrajectoryRecord:
    """Use a wall-clock point only when the source supplied no timing."""
    if _has_timing(record.timing):
        return record
    if previous is not None and _has_timing(previous.timing):
        previous_timing = previous.timing
        assert previous_timing is not None
        if previous_timing.provenance is TimingProvenance.OBSERVED:
            timing = _advance_observed(previous_timing, record, observed_at)
        else:
            timing = previous_timing
        return replace(record, timing=timing)
    timing = (
        Timing(end=observed_at, provenance=TimingProvenance.OBSERVED)
        if _is_terminal_point(record)
        else Timing(start=observed_at, provenance=TimingProvenance.OBSERVED)
    )
    return replace(record, timing=timing)


def _advance_observed(timing: Timing, record: TrajectoryRecord, observed_at: float) -> Timing:
    if not _is_terminal_point(record) or timing.end is not None:
        return timing
    if timing.start is None:
        return Timing(end=observed_at, provenance=TimingProvenance.OBSERVED)
    if observed_at <= timing.start:
        return timing
    return Timing(
        start=timing.start,
        end=observed_at,
        duration_ms=(observed_at - timing.start) * 1000,
        provenance=TimingProvenance.OBSERVED,
        first_token=timing.first_token,
    )


def _is_terminal_point(record: TrajectoryRecord) -> bool:
    return record.kind in {
        TrajectoryKind.TOOL_RESULT,
        TrajectoryKind.THEATER_RESULT,
        TrajectoryKind.ERROR,
    } or (record.lane is TrajectoryLane.MODEL and record.status in _TERMINAL_STATUSES)


def _has_timing(timing: Timing | None) -> bool:
    return timing is not None and any(
        value is not None
        for value in (timing.start, timing.end, timing.duration_ms, timing.first_token)
    )


def _row_id(row: Mapping[str, object]) -> int | None:
    value = row.get("id")
    return value if type(value) is int and value >= 0 else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = [
    "ObservationPoint",
    "apply_live_observation",
    "apply_observation_points",
    "observation_points",
    "observation_points_for_history",
    "with_observed_time",
]
