"""Pure exact-link and stable-correlation trajectory projection."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from theater.constants.trajectory import TRAJECTORY_THEATER_BUS_RECORD_PREFIX
from theater.trajectory.grouping import newer_record
from theater.trajectory.records import ParticipantLink, TrajectoryRecord


class CausalResolution(StrEnum):
    EXACT = "exact"
    PARTICIPANT = "participant"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class ResolvedParticipantLink:
    link: ParticipantLink
    resolution: CausalResolution
    target: TrajectoryRecord | None = None


class CausalIndex:
    def __init__(self, records: Iterable[TrajectoryRecord]) -> None:
        values = tuple(records)
        if any(not isinstance(record, TrajectoryRecord) for record in values):
            raise TypeError("records must contain TrajectoryRecord values")
        self._records = _select_records(values)
        correlations: dict[tuple[str, str], list[TrajectoryRecord]] = {}
        for record in self._records.values():
            for link in record.links:
                if link.correlation_type is not None:
                    assert link.correlation_key is not None
                    key = link.correlation_type, link.correlation_key
                    correlations.setdefault(key, []).append(record)
        self._correlations = {
            key: tuple(sorted(_unique(records), key=_record_key))
            for key, records in correlations.items()
        }

    def resolve(self, link: ParticipantLink) -> ResolvedParticipantLink:
        if link.target_record_id is None:
            return ResolvedParticipantLink(link, CausalResolution.PARTICIPANT)
        target = self._records.get((link.participant_id, link.target_record_id))
        if target is None:
            return ResolvedParticipantLink(link, CausalResolution.UNRESOLVED)
        return ResolvedParticipantLink(link, CausalResolution.EXACT, target)

    def links_for(self, record: TrajectoryRecord) -> tuple[ResolvedParticipantLink, ...]:
        return tuple(self.resolve(link) for link in record.links)

    def related_records(self, record: TrajectoryRecord) -> tuple[TrajectoryRecord, ...]:
        related: dict[tuple[str, str], TrajectoryRecord] = {}
        for resolved in self.links_for(record):
            if resolved.target is not None:
                related[_identity(resolved.target)] = resolved.target
            link = resolved.link
            if link.correlation_type is not None:
                assert link.correlation_key is not None
                for candidate in self._correlations.get(
                    (link.correlation_type, link.correlation_key), ()
                ):
                    if candidate != record:
                        related[_identity(candidate)] = candidate
        return tuple(sorted(related.values(), key=_record_key))


def _identity(record: TrajectoryRecord) -> tuple[str, str]:
    return record.participant_id, record.record_id


def _unique(records: Iterable[TrajectoryRecord]) -> tuple[TrajectoryRecord, ...]:
    return tuple(_select_records(records).values())


def _select_records(
    records: Iterable[TrajectoryRecord],
) -> dict[tuple[str, str], TrajectoryRecord]:
    selected: dict[tuple[str, str], TrajectoryRecord] = {}
    for record in sorted(records, key=_record_key):
        key = _identity(record)
        selected[key] = newer_record(selected[key], record) if key in selected else record
    return {
        key: selected[key] for key in sorted(selected, key=lambda item: _record_key(selected[item]))
    }


def _record_key(record: TrajectoryRecord) -> tuple[object, ...]:
    if record.record_id.startswith(TRAJECTORY_THEATER_BUS_RECORD_PREFIX):
        row_id = record.record_id.removeprefix(TRAJECTORY_THEATER_BUS_RECORD_PREFIX)
        if row_id.isdecimal():
            return 0, 0, int(row_id), record.participant_id, record.record_id
        return 0, 1, row_id, record.participant_id, record.record_id
    coordinate = record.source_offset if record.source_offset is not None else record.raw_index
    return (
        1,
        0,
        record.source_epoch,
        coordinate,
        record.event_ordinal,
        record.participant_id,
        record.record_id,
    )


__all__ = ["CausalIndex", "CausalResolution", "ResolvedParticipantLink"]
