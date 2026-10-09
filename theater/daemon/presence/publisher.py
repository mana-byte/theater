"""Coalesced presence change publication; safety stays in snapshots and revisions."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence

logger = logging.getLogger("theater.daemon.presence")

PRESENCE_PUBLISH_COALESCE_SECONDS = 0.25


class PresenceChangePublisher:
    """Batch change notifications within a short window, one per participant."""

    def __init__(
        self,
        emit: Callable[[str], None],
        *,
        window: float = PRESENCE_PUBLISH_COALESCE_SECONDS,
    ) -> None:
        self._emit = emit
        self._window = window
        self._pending: dict[str, None] = {}
        self._timer: asyncio.TimerHandle | None = None
        self._closed = False

    def publish(self, participant_id: str) -> None:
        if self._closed:
            return
        self._pending[participant_id] = None
        self._arm()

    def flush(self) -> None:
        self._disarm()  # a fired timer must never block the next window
        if self._closed or not self._pending:
            return
        pending, self._pending = tuple(self._pending), {}
        self._deliver(pending)

    def close(self) -> None:
        """Cancel the timer and drop pending notifications; never publish after."""
        self._closed = True
        self._disarm()
        self._pending.clear()

    def _deliver(self, participant_ids: Sequence[str]) -> None:
        for participant_id in participant_ids:
            try:
                self._emit(participant_id)
            except Exception:
                logger.exception("publishing presence change for %s failed", participant_id)

    def _arm(self) -> None:
        if self._timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()  # no loop to defer on: publish now instead of losing it
            return
        self._timer = loop.call_later(self._window, self.flush)

    def _disarm(self) -> None:
        if self._timer is not None:
            self._timer.cancel()  # a no-op once the handle has already fired
            self._timer = None


__all__ = ["PRESENCE_PUBLISH_COALESCE_SECONDS", "PresenceChangePublisher"]
