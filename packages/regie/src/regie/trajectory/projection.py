"""Bounded, Textual-free projection of additive public trajectory records."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from regie.render import bounded_text
from regie.trajectory.controller import TrajectoryState


@dataclass(frozen=True, slots=True)
class TrajectoryRow:
    record_id: str
    revision: int
    kind: str
    summary: str


def rows_for_state(state: TrajectoryState) -> tuple[TrajectoryRow, ...]:
    """Project known display fields while retaining unknown wire values in controller state."""
    rows: list[TrajectoryRow] = []
    for record in state.records.values():
        rows.append(_row(record))
    rows.sort(key=lambda row: (row.revision, row.record_id))
    return tuple(rows)


def _row(record: Mapping[str, object]) -> TrajectoryRow:
    record_id = record.get("record_id")
    if not isinstance(record_id, str) or not record_id:
        raise TypeError("trajectory record lacks a usable record_id")
    revision = record.get("revision")
    revision_value = revision if type(revision) is int and revision >= 0 else 0
    kind = next(
        (
            value
            for key in ("kind", "event_kind", "type")
            if isinstance((value := record.get(key)), str) and value
        ),
        "event",
    )
    summary = next(
        (
            value
            for key in ("summary", "message", "name", "text")
            if isinstance((value := record.get(key)), str) and value
        ),
        kind,
    )
    return TrajectoryRow(record_id, revision_value, kind, bounded_text(summary))


__all__ = ["TrajectoryRow", "rows_for_state"]
