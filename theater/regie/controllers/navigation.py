"""Footer keyboard-navigation state machine.

Owns the two pieces of state that drive footer metric navigation: which
metric the keyboard cursor is on (``None`` means the tree owns the
cursor) and where the user came from when they descended into the footer
(so Up can return to the remembered column rather than a default).

The controller is pure state plus transition methods that return
explicit decision values. It holds no reference to the app, Textual
widgets, the daemon client, or tmux. The app performs all side effects
(rendering, panel sync, cursor reactive changes) in the same order as
before, using the transition result to decide what to do.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from theater.constants.regie import (
    REGIE_USAGE_METRIC_DOWN,
    REGIE_USAGE_METRIC_LEFT,
    REGIE_USAGE_METRIC_RIGHT,
    REGIE_USAGE_METRIC_UP,
)


class UpDecision(Enum):
    """Outcome of ``NavigationState.up``: select a metric, leave footer, or decrement tree."""

    LEAVE = "leave"


@dataclass
class NavigationState:
    """Footer keyboard-navigation state, independent of the app."""

    metric: str | None = None
    origin: str | None = None

    @property
    def in_footer(self) -> bool:
        """Whether the keyboard cursor is in the footer rather than the tree."""
        return self.metric is not None

    def down(self) -> str | None:
        """Transition for Down: footer metric move or tree-to-footer entry.

        Returns the new metric to select, or ``None`` when the caller should
        advance the tree cursor instead. When the metric is already set, a
        found target records the current metric as origin before returning;
        a missing target (cost/average) returns the current metric unchanged
        as a no-op signal. When no metric is set, returns ``"input"`` to
        signal entering the footer, and clears origin.
        """
        if self.metric is not None:
            target = REGIE_USAGE_METRIC_DOWN.get(self.metric)
            if target is not None:
                self.origin = self.metric
                self.metric = target
            return self.metric
        self.origin = None
        self.metric = "input"
        return self.metric

    def up(self) -> str | UpDecision | None:
        """Transition for Up: return to origin, leave footer, or tree decrement.

        Returns the metric to select, ``UpDecision.LEAVE`` when the caller
        should leave the footer entirely, or ``None`` when the caller should
        decrement the tree cursor. When in the footer, cost/average return
        to the remembered origin (or default mapping) and clear origin;
        input/output/cache leave the footer. When not in the footer, returns
        ``None`` to signal a tree decrement.
        """
        if self.metric is not None:
            if self.metric in REGIE_USAGE_METRIC_UP:
                target = self.origin or REGIE_USAGE_METRIC_UP[self.metric]
                self.origin = None
                self.metric = target
                return target
            self.metric = None
            self.origin = None
            return UpDecision.LEAVE
        return None

    def left(self) -> str | None:
        """Transition for Left: move within footer, or no-op outside it.

        Returns the new metric to select and clears origin, or ``None``
        when the footer is not active or the mapping has no target.
        """
        if self.metric is None:
            return None
        target = REGIE_USAGE_METRIC_LEFT.get(self.metric)
        if target is not None:
            self.origin = None
            self.metric = target
            return target
        return None

    def right(self) -> str | None:
        """Transition for Right: move within footer, or no-op outside it.

        Returns the new metric to select and clears origin, or ``None``
        when the footer is not active or the mapping has no target.
        """
        if self.metric is None:
            return None
        target = REGIE_USAGE_METRIC_RIGHT.get(self.metric)
        if target is not None:
            self.origin = None
            self.metric = target
            return target
        return None

    def select(self, metric: str) -> None:
        """Set the active metric without side effects (the app renders/syncs)."""
        self.metric = metric

    def leave(self) -> None:
        """Clear both metric and origin (the app renders/syncs)."""
        self.metric = None
        self.origin = None
