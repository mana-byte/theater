"""Mutable data owned by one warm trajectory stream."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field

from theater.daemon.trajectory.cache import CacheStream
from theater.daemon.trajectory.merge import order_records
from theater.daemon.trajectory.observed_timing import ObservationPoint
from theater.harness.contracts.source import Batch
from theater.models import Participant
from theater.trajectory import CoverageGap, PanelStateInfo, TrajectoryCapabilities, TrajectoryRecord


@dataclass(frozen=True, slots=True)
class CapturedBatch:
    serial: int
    batch: Batch
    observed_at: float


@dataclass(slots=True)
class TrajectoryStream:
    participant: Participant
    cache: CacheStream
    panel_state: PanelStateInfo
    source_epoch: str | None = None
    transcript_floor: str | None = None
    theater_floor: str | None = None
    source_before: str | None = None
    bus_before: int | None = None
    gaps: list[CoverageGap] = field(default_factory=list)
    declared_capabilities: TrajectoryCapabilities = field(default_factory=TrajectoryCapabilities)
    live_updates_observed: bool = False
    pending_live: list[CapturedBatch] = field(default_factory=list)
    observation_points: tuple[ObservationPoint, ...] = ()
    followers: dict[int, asyncio.Event] = field(default_factory=dict)
    capture_serial: int = 0
    initialized: bool = False
    trusted: bool = False
    live_allowed: bool = False
    pending_wake: asyncio.TimerHandle | None = None
    initialization_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def ring(self):
        return self.cache.ring


def count_records_before(records: Iterable[TrajectoryRecord], marker: str) -> int:
    ordered = order_records(records)
    try:
        return next(index for index, record in enumerate(ordered) if record.record_id == marker)
    except StopIteration:
        return 0


__all__ = ["CapturedBatch", "TrajectoryStream", "count_records_before"]
