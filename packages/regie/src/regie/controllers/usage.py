"""Pointer and keyboard state for the usage overlay."""

from __future__ import annotations

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

    def sync(self) -> SyncOutcome:
        if self.resolve_metric() is not None:
            return SyncOutcome.ACTIVATE
        if self.active_metric is not None:
            return SyncOutcome.CLOSE
        return SyncOutcome.NO_OP

    def clear_active(self) -> None:
        self.active_metric = None

    def select_keyboard(self, metric: str) -> None:
        self.keyboard_metric = metric

    def leave_keyboard(self) -> None:
        self.keyboard_metric = None
        self.keyboard_origin = None


__all__ = ["ActivateOutcome", "SyncOutcome", "UsagePanelState"]
