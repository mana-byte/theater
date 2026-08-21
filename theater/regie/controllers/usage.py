"""Usage-panel state and daemon usage queries.

``UsagePanelState`` owns overlay state: pointer/active metric, cached
breakdown result, error message, and a generation counter for stale
rejection. ``UsageQueries`` owns daemon usage RPC calls plus
compatibility/error classification. Neither holds a reference to the app,
Textual widgets, or tmux.

The app constructs ``UsageQueries`` from its connected ``DaemonClient``
and performs all DOM operations, panel rendering, worker launches, and
exception suppression around widgets.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from theater.client import DaemonClient
from theater.constants import USAGE_AVERAGE_WINDOW_DAYS, USAGE_AVERAGE_WINDOW_HOURS
from theater.protocol import RemoteError

logger = logging.getLogger("theater.regie")


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


@dataclass
class BreakdownResult:
    """Result of a per-harness usage breakdown fetch."""

    result: dict | None = None
    message: str | None = None


@dataclass
class SummaryResult:
    """Result of a usage summary fetch.

    ``raw`` is the daemon response (may be non-dict); ``available`` is False
    when the RPC failed entirely.
    """

    raw: Any = None
    available: bool = True


class UsageQueries:
    """Daemon usage RPC calls and error classification, no app reference."""

    def __init__(self, client: DaemonClient) -> None:
        self._client = client

    async def fetch_breakdown(self) -> BreakdownResult:
        """Fetch per-harness usage, classifying errors into user-facing messages."""
        result: dict | None = None
        message: str | None = None
        try:
            response = await self._client.call("usage_by_harness")
            if isinstance(response, dict):
                result = response
            else:
                logger.debug(
                    "per-harness usage returned %s, expected dict",
                    type(response).__name__,
                )
                message = "per-harness stats unavailable"
        except RemoteError as exc:
            if exc.code == "unknown_method":
                message = "restart daemon for per-harness stats"
            else:
                logger.debug("per-harness usage unavailable: %s", exc)
                message = "per-harness stats unavailable"
        except Exception as exc:
            logger.debug("per-harness usage unavailable: %s", exc)
            message = "per-harness stats unavailable"
        return BreakdownResult(result=result, message=message)

    async def fetch_summary(self, *, window: float, period: str) -> SummaryResult:
        """Fetch usage_summary, falling back to legacy on unknown_method.

        Returns ``SummaryResult(raw=..., available=True)`` on success or
        fallback (``raw`` may be non-dict), or ``SummaryResult(available=False)``
        when the RPC fails entirely.
        """
        try:
            summary = await self._client.call("usage_summary", window=window, period=period)
        except RemoteError as exc:
            if exc.code != "unknown_method":
                logger.debug("usage refresh failed: %s", exc)
                return SummaryResult(available=False)
            try:
                summary = await self.legacy_summary(window=window)
            except Exception as fallback_exc:
                logger.debug("legacy usage refresh failed: %s", fallback_exc)
                return SummaryResult(available=False)
        except Exception as exc:
            logger.debug("usage refresh failed: %s", exc)
            return SummaryResult(available=False)
        return SummaryResult(raw=summary)

    async def legacy_summary(self, *, window: float) -> dict:
        """Compatibility with a pre-upgrade daemon that lacks usage_summary."""
        windowed = await self._client.call("usage_totals", window=window)
        average = await self._client.call("usage_totals", window=USAGE_AVERAGE_WINDOW_HOURS)
        if isinstance(average, dict):
            average = {**average, "active_days": USAGE_AVERAGE_WINDOW_DAYS}
        return {"period": None, "windowed": windowed, "average": average}
