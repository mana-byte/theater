"""Typed stream mutations shared by trajectory ingestion."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from theater.daemon.trajectory.cache import RecordChange
from theater.daemon.trajectory.stream import TrajectoryStream
from theater.trajectory import PanelState, TrajectoryRecord


class MergeRecords(Protocol):
    def __call__(
        self,
        stream: TrajectoryStream,
        records: Iterable[TrajectoryRecord],
        *,
        notify: bool,
    ) -> tuple[RecordChange, ...]: ...


class AddGap(Protocol):
    def __call__(
        self,
        stream: TrajectoryStream,
        source: str,
        reason: str,
        start: str | None = None,
        end: str | None = None,
    ) -> bool: ...


class AddBoundary(Protocol):
    def __call__(self, stream: TrajectoryStream, old: str, new: str) -> None: ...


class SetPanel(Protocol):
    def __call__(
        self,
        stream: TrajectoryStream,
        state: PanelState,
        message: str,
        *,
        notify: bool,
    ) -> bool: ...


class WakeFollowers(Protocol):
    def __call__(self, stream: TrajectoryStream) -> None: ...


@dataclass(frozen=True, slots=True)
class TrajectoryMutationHooks:
    merge_records: MergeRecords
    add_gap: AddGap
    add_boundary: AddBoundary
    set_panel: SetPanel
    wake_followers: WakeFollowers


__all__ = [
    "AddBoundary",
    "AddGap",
    "MergeRecords",
    "SetPanel",
    "TrajectoryMutationHooks",
    "WakeFollowers",
]
