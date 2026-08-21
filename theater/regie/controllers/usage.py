"""Usage-panel state: pointer/active metric, cached breakdown, generation.

Owns the five pieces of state that drive the per-harness usage breakdown
overlay: which metric the pointer is hovering, which metric is active,
the cached fetch result and error message, and a generation counter for
stale-response rejection.

The controller is pure state plus transition methods that return explicit
decision values. It holds no reference to the app, Textual widgets, the
daemon client, or tmux. The app performs all DOM operations, panel
rendering, worker launches, and error logging, using the transition
result to decide what to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ActivateOutcome(Enum):
    """What ``activate`` needs the app to do after a state transition."""

    FIRST_OPEN = "first_open"
    SWITCH = "switch"
    NO_CHANGE = "no_change"


class SyncOutcome(Enum):
    """What ``sync`` needs the app to do after a state transition."""

    ACTIVATE = "activate"
    CLOSE = "close"
    NO_OP = "no_op"


class FetchAccept(Enum):
    """Whether ``accept_fetch`` accepted or rejected the late response."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass
class UsagePanelState:
    """Usage breakdown overlay state, independent of the app."""

    pointer_metric: str | None = None
    active_metric: str | None = None
    breakdown: dict | None = None
    message: str | None = None
    generation: int = 0

    def resolve_metric(self, keyboard_metric: str | None) -> str | None:
        """Pointer wins over keyboard; None means no selection."""
        return self.pointer_metric or keyboard_metric

    def activate(self, metric: str) -> ActivateOutcome:
        """Set active metric and report what side effects the app should do.

        Returns ``FIRST_OPEN`` when this is the first activation (app should
        constrain, increment generation, clear cache, render loading, and
        launch a fetch), ``SWITCH`` when the metric changed within an active
        session (app should render from cache), or ``NO_CHANGE`` when the
        metric is already active.
        """
        previous = self.active_metric
        first = previous is None
        self.active_metric = metric
        if first:
            return ActivateOutcome.FIRST_OPEN
        if previous != metric:
            return ActivateOutcome.SWITCH
        return ActivateOutcome.NO_CHANGE

    def begin_first_open(self) -> int:
        """Clear cache and increment generation for a new fetch. Return generation."""
        self.generation += 1
        self.breakdown = None
        self.message = None
        return self.generation

    def clear(self) -> None:
        """Reset active metric, cache, and message; increment generation."""
        self.active_metric = None
        self.breakdown = None
        self.message = None
        self.generation += 1

    def sync(self, keyboard_metric: str | None) -> SyncOutcome:
        """Resolve metric and report which transition the app should perform.

        Returns ``ACTIVATE`` when a metric is resolved and the app should call
        ``activate``, ``CLOSE`` when no metric is resolved but the panel is
        active (app should clear hot tiles, call ``clear``, then hide panel),
        or ``NO_OP`` when the panel is already inactive.
        """
        metric = self.resolve_metric(keyboard_metric)
        if metric is not None:
            return SyncOutcome.ACTIVATE
        if self.active_metric is None:
            return SyncOutcome.NO_OP
        return SyncOutcome.CLOSE

    def accept_fetch(
        self, *, generation: int, result: dict | None, message: str | None
    ) -> FetchAccept:
        """Cache a fetched result if the generation is current and the panel is active.

        Returns ``ACCEPTED`` when cached (app should render the current
        metric) or ``REJECTED`` when the generation is stale or the panel is
        inactive.
        """
        if generation != self.generation or self.active_metric is None:
            return FetchAccept.REJECTED
        self.breakdown = result
        self.message = message
        return FetchAccept.ACCEPTED

    def set_pointer(self, metric: str | None) -> None:
        """Set the pointer hover metric."""
        self.pointer_metric = metric
