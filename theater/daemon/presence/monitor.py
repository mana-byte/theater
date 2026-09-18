"""Provider-backed human-presence monitor."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from theater.constants.presence import (
    PRESENCE_CLOSE_TIMEOUT_SECONDS,
    PRESENCE_INVENTORY_STALE_SECONDS,
    PRESENCE_REFRESH_INTERVAL_SECONDS,
    PRESENCE_REFRESH_TIMEOUT_SECONDS,
)
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.daemon.presence.provider import ExitHandler, ProviderPresenceSource
from theater.models import HumanPresent, NotFound

_AWAIT_GUIDANCE = "call await_sessions(handles=[{participant_id!r}]), then retry"
logger = logging.getLogger("theater.daemon.presence")


class PresenceMonitor:
    """Cache exact provider inspection evidence and fail closed on route loss."""

    def __init__(
        self,
        registry,
        *,
        refresh_interval: float = PRESENCE_REFRESH_INTERVAL_SECONDS,
        stale_after: float = PRESENCE_INVENTORY_STALE_SECONDS,
        arm_check_interval: float | None = None,
        clock=time.monotonic,
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        del arm_check_interval
        self._registry = registry
        self._refresh_interval = refresh_interval
        self._stale_after = stale_after
        self._clock = clock
        self._on_change = on_change
        self._revision = 0
        self._revision_event = asyncio.Event()
        self._published_states: dict[str, PresenceState] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._loop_task: asyncio.Task[None] | None = None
        self._provider = ProviderPresenceSource(
            registry,
            clock=clock,
            refresh_timeout=PRESENCE_REFRESH_TIMEOUT_SECONDS,
        )
        self._stopping = False

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def observed_at(self) -> float | None:
        snapshots = self._provider.observed_at_values()
        return max(snapshots) if snapshots else None

    def snapshot(self, participant_id: str) -> PresenceSnapshot:
        if self._stopping:
            return PresenceSnapshot(PresenceState.UNKNOWN, "monitor-closed", self._revision, None)
        try:
            self._registry.get(participant_id)
        except NotFound:
            return PresenceSnapshot(PresenceState.ABSENT, "unregistered", self._revision, None)
        provider = self._provider.snapshot(
            participant_id,
            revision=self._revision,
            stale_after=self._stale_after,
        )
        if provider is not None:
            return provider
        # Historical RC9 pane fields and a healthy native route do not prove
        # that no human is present.  Missing applicable evidence protects.
        return PresenceSnapshot(
            PresenceState.UNKNOWN,
            "no-terminal-presence-evidence",
            self._revision,
            None,
        )

    async def refresh(self) -> None:
        if self._stopping:
            return
        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._refresh_owned(), name="presence-refresh-once")
            self._refresh_task = task
        await asyncio.shield(task)

    async def require_absent(self, participant_id: str) -> None:
        try:
            self._registry.get(participant_id)
        except NotFound:
            return
        guidance = _AWAIT_GUIDANCE.format(participant_id=participant_id)
        await self.refresh()
        snapshot = self.snapshot(participant_id)
        if snapshot.state is not PresenceState.ABSENT:
            raise HumanPresent(
                f"human presence for {participant_id!r} is {snapshot.state.value} "
                f"({snapshot.reason}); not mutating; {guidance}"
            )

    def configure_terminal_service(
        self,
        terminal_service,
        *,
        exit_handler: ExitHandler | None = None,
    ) -> None:
        self._provider.configure(terminal_service, exit_handler=exit_handler)

    def terminal_screen(self, participant_id: str) -> str | None:
        return self._provider.screen(participant_id, stale_after=self._stale_after)

    async def wait_for_change(self, after_revision: int) -> int:
        while self._revision <= after_revision:
            event = self._revision_event
            await event.wait()
        return self._revision

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        self._stopping = False
        await self.refresh()
        self._loop_task = asyncio.create_task(self._loop(), name="presence-refresh")

    async def reconcile(self) -> None:
        await self.refresh()

    async def aclose(self) -> None:
        self._stopping = True
        tasks = [task for task in (self._loop_task, self._refresh_task) if task is not None]
        try:
            async with asyncio.timeout(PRESENCE_CLOSE_TIMEOUT_SECONDS):
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            pass
        self._loop_task = self._refresh_task = None

    async def _refresh_owned(self) -> None:
        participants = tuple(self._registry.list())
        before = {
            item.id: self._published_states.get(item.id, self.snapshot(item.id).state)
            for item in participants
        }
        await self._provider.refresh(participants)
        if not self._stopping:
            self._bump_revision()
            for participant in participants:
                after = self.snapshot(participant.id).state
                self._published_states[participant.id] = after
                if before[participant.id] is after:
                    continue
                try:
                    if self._on_change is not None:
                        self._on_change(participant.id)
                except Exception:
                    logger.exception("publishing presence change for %s failed", participant.id)
            live_ids = {participant.id for participant in participants}
            for participant_id in self._published_states.keys() - live_ids:
                self._published_states.pop(participant_id, None)

    def _bump_revision(self) -> None:
        self._revision += 1
        pending = self._revision_event
        self._revision_event = asyncio.Event()
        pending.set()

    async def _loop(self) -> None:
        while not self._stopping:
            await asyncio.sleep(self._refresh_interval)
            await self.refresh()


__all__ = ["PresenceMonitor"]
