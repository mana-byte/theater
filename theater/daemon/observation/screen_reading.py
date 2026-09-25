"""Provider screen reads: the parser-less screen watch and reducer screen glue."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from theater.constants.observation import SCREEN_CAPTURE_MAX_BYTES
from theater.daemon.observation.reducer import QuietClock, Reducer
from theater.harness import Harness, HarnessObserver
from theater.models import Status

if TYPE_CHECKING:
    from theater.daemon.store import Store

logger = logging.getLogger("theater.observer")


class ScreenReading:
    if TYPE_CHECKING:
        harnesses: dict[str, Harness]
        _reducer: Reducer
        screen: float
        _stopping: asyncio.Event
        store: Store
        _terminal_evidence_provider: Any
        _answer_turn: Callable[..., Any]
        _discard_agent_telemetry: Callable[..., Any]
        _settle: Callable[..., Any]
        _sleep: Callable[..., Any]

    async def _watch_screen(self, pid: str, harness_name: str) -> None:
        """Derive status from the rendered screen, for a parser-less harness."""
        from theater.constants.observation import IDLE_CONFIRMATIONS

        observer = self.harnesses[harness_name].observer
        idle_streak = 0
        ended = False
        try:
            while not self._stopping.is_set():
                try:
                    p = self.store.get_participant(pid)
                    if p is None or p.status is Status.DEAD:
                        return
                    capture = await self._capture(pid)
                    if capture is not None:
                        idle_streak = idle_streak + 1 if observer.is_idle_screen(capture) else 0
                        if idle_streak >= IDLE_CONFIRMATIONS:
                            if not ended:
                                ended = True
                                self._end_turn_from_screen(pid, capture)
                            self._settle(pid, Status.IDLE)
                        elif idle_streak == 0:
                            ended = False
                            self._settle(pid, Status.WORKING)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("observing screen of %s failed", pid)
                await self._sleep(self.screen)
        finally:
            self._discard_agent_telemetry(pid)

    async def _capture(self, participant_id: str) -> str | None:
        """Refresh and return identity-fenced provider screen evidence."""
        provider = self._terminal_evidence_provider
        if provider is None:
            return None
        try:
            return await provider.capture_screen(participant_id, max_bytes=SCREEN_CAPTURE_MAX_BYTES)
        except Exception:
            return None

    def _provider_screen(self, participant_id: str) -> str | None:
        provider = self._terminal_evidence_provider
        if provider is None:
            return None
        try:
            return provider.terminal_screen(participant_id)
        except Exception:
            return None

    async def _capture_for_reducer(self, participant_id: str) -> str | None:
        """Read _capture at call-time so instance monkeypatches take effect."""
        return await self._capture(participant_id)

    async def _screen_only(
        self,
        pid: str,
        observer: HarnessObserver,
        clock: QuietClock,
        *,
        source_status: Status | None = None,
    ) -> None:
        await self._reducer.screen_only(
            pid,
            observer,
            clock,
            source_status=source_status,
        )

    async def _screen_status_due(
        self,
        pid: str,
        observer: HarnessObserver,
        clock: QuietClock,
        *,
        source_status: Status | None = None,
    ) -> None:
        await self._reducer._screen_status_due(
            pid,
            observer,
            clock,
            source_status=source_status,
        )

    async def _check_idle_screen(self, pid: str, observer: HarnessObserver) -> None:
        await self._reducer.check_idle_screen(pid, observer)

    def _end_turn_from_screen(self, pid: str, capture: str) -> None:
        self._reducer.end_turn_from_screen(pid, capture, answer_turn_fn=self._answer_turn)
