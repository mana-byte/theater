"""Pointer and keyboard state for the usage overlay."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum


class ActivateOutcome(Enum):
    FIRST_OPEN = "first_open"
    SWITCH = "switch"
    NO_CHANGE = "no_change"


class SyncOutcome(Enum):
    ACTIVATE = "activate"
    CLOSE = "close"
    NO_OP = "no_op"


class FetchAccept(Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


def participant_costs(result: Mapping[str, object]) -> dict[str, int]:
    rows = result.get("participants")
    if not isinstance(rows, list):
        return {}
    return {
        str(row["participant_id"]): int(row["cost_microcents"])
        for row in rows
        if isinstance(row, dict)
        and isinstance(row.get("participant_id"), str)
        and type(row.get("cost_microcents")) is int
    }


@dataclass
class UsagePanelState:
    """Presentation state shared by all five usage metrics."""

    pointer_metric: str | None = None
    keyboard_metric: str | None = None
    keyboard_origin: str | None = None
    active_metric: str | None = None
    breakdown: dict | None = None
    message: str | None = None
    detailed: bool = False
    detailed_breakdown: dict | None = None
    detailed_message: str | None = None
    detailed_attempted: bool = False
    detailed_fetching: bool = False
    compact_fetching: bool = False
    participant_usage: list[dict] | None = None
    generation: int = 0

    @property
    def in_footer(self) -> bool:
        return self.keyboard_metric is not None

    def resolve_metric(self) -> str | None:
        return self.pointer_metric or self.keyboard_metric

    def activate(self, metric: str) -> ActivateOutcome:
        previous = self.active_metric
        self.active_metric = metric
        if previous is None:
            return ActivateOutcome.FIRST_OPEN
        return ActivateOutcome.SWITCH if previous != metric else ActivateOutcome.NO_CHANGE

    def begin_first_open(self) -> int:
        """Start a new overlay lifetime with no data inherited from the last open."""
        self.generation += 1
        self.compact_fetching = False
        self.detailed_fetching = False
        self.detailed_attempted = False
        self.breakdown = None
        self.message = None
        self.detailed_breakdown = None
        self.detailed_message = None
        return self.generation

    def begin_compact_fetch(self) -> int | None:
        if (
            self.active_metric is None
            or self.compact_fetching
            or self.breakdown is not None
            or self.message is not None
        ):
            return None
        self.compact_fetching = True
        return self.generation

    def begin_detailed_fetch(self) -> int | None:
        if (
            not self.detailed
            or self.active_metric is None
            or self.detailed_attempted
            or self.detailed_fetching
        ):
            return None
        self.detailed_fetching = True
        return self.generation

    def toggle_detailed(self) -> bool:
        self.detailed = not self.detailed
        return self.detailed

    def sync(self) -> SyncOutcome:
        if self.resolve_metric() is not None:
            return SyncOutcome.ACTIVATE
        if self.active_metric is not None:
            return SyncOutcome.CLOSE
        return SyncOutcome.NO_OP

    def clear_active(self) -> None:
        """Close the overlay and invalidate all in-flight responses."""
        self.active_metric = None
        self.breakdown = None
        self.message = None
        self.detailed_breakdown = None
        self.detailed_message = None
        self.detailed_attempted = False
        self.compact_fetching = False
        self.detailed_fetching = False
        self.generation += 1

    def leave_keyboard(self) -> None:
        self.keyboard_metric = None
        self.keyboard_origin = None

    def update_participants(
        self, result: dict[str, object], *, names: Mapping[str, str] | None = None
    ) -> None:
        rows = result.get("participants")
        if not isinstance(rows, list):
            self.participant_usage = None
            return
        labels = names or {}
        self.participant_usage = [
            {**row, "name": labels.get(str(row.get("participant_id")), row.get("participant_id"))}
            for row in rows
            if isinstance(row, dict)
        ]

    def accept_fetch(
        self, *, generation: int, result: dict | None, message: str | None
    ) -> FetchAccept:
        if generation != self.generation:
            return FetchAccept.REJECTED
        self.compact_fetching = False
        if self.active_metric is None:
            return FetchAccept.REJECTED
        self.breakdown = result
        self.message = message
        return FetchAccept.ACCEPTED

    def accept_detailed_fetch(
        self, *, generation: int, result: dict | None, message: str | None
    ) -> FetchAccept:
        if generation != self.generation or self.active_metric is None:
            return FetchAccept.REJECTED
        self.detailed_fetching = False
        self.detailed_attempted = True
        self.detailed_breakdown = result
        self.detailed_message = message
        return FetchAccept.ACCEPTED


__all__ = [
    "ActivateOutcome",
    "FetchAccept",
    "SyncOutcome",
    "UsagePanelState",
    "participant_costs",
]
