"""Provider-backed human-presence monitor."""

from __future__ import annotations

import asyncio
import contextlib
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
from theater.models import HumanPresent, NotFound, TerminalBindingRecord

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
        self._wake = asyncio.Event()
        self._published_states: dict[str, PresenceState] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._target_tasks: dict[str, asyncio.Task[None]] = {}
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

    def snapshot_for_binding(
        self,
        participant_id: str,
        binding: TerminalBindingRecord | None,
        *,
        allow_reconciling: bool = False,
    ) -> PresenceSnapshot:
        """Project presence against a binding read inside the caller's transaction."""
        if self._stopping:
            return PresenceSnapshot(PresenceState.UNKNOWN, "monitor-closed", self._revision, None)
        provider = self._provider.snapshot_for_binding(
            participant_id,
            binding,
            revision=self._revision,
            stale_after=self._stale_after,
            allow_reconciling=allow_reconciling,
        )
        if provider is not None:
            return provider
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
        await self._refresh_target(participant_id, fresh=True)
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

    def invalidate_provider(self, provider_id: str, generation: int) -> None:
        if self._stopping:
            return
        invalidated = self._provider.invalidate(provider_id, generation)
        if invalidated is None:
            return
        self._bump_revision()
        for participant_id in invalidated:
            before = self._published_states.get(participant_id)
            self._published_states[participant_id] = PresenceState.UNKNOWN
            if before is not PresenceState.UNKNOWN:
                self._publish_change(participant_id)
        self._wake.set()

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
        tasks = [
            task
            for task in (self._loop_task, self._refresh_task, *self._target_tasks.values())
            if task is not None
        ]
        try:
            async with asyncio.timeout(PRESENCE_CLOSE_TIMEOUT_SECONDS):
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            pass
        self._loop_task = self._refresh_task = None
        self._target_tasks.clear()

    async def _refresh_owned(self) -> None:
        participants = tuple(self._registry.list())
        await asyncio.gather(*(self._refresh_target(item.id) for item in participants))
        if not self._stopping:
            if not participants:
                self._bump_revision()
            live_ids = {participant.id for participant in participants}
            for participant_id in self._published_states.keys() - live_ids:
                self._published_states.pop(participant_id, None)

    async def _refresh_target(self, participant_id: str, *, fresh: bool = False) -> None:
        if self._stopping:
            return
        task = self._target_tasks.get(participant_id)
        if fresh and task is not None and not task.done():
            # Admission must not reuse focus evidence gathered before this request.
            await asyncio.shield(task)
            if self._stopping:
                return
            task = self._target_tasks.get(participant_id)
        if task is None or task.done():
            task = asyncio.create_task(
                self._refresh_participant(participant_id), name=f"presence-refresh-{participant_id}"
            )
            self._target_tasks[participant_id] = task
            task.add_done_callback(lambda done: self._forget_target(participant_id, done))
        await asyncio.shield(task)

    def _forget_target(self, participant_id: str, task: asyncio.Task[None]) -> None:
        if self._target_tasks.get(participant_id) is task:
            self._target_tasks.pop(participant_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "presence refresh failed for %s", participant_id, exc_info=task.exception()
            )

    async def _refresh_participant(self, participant_id: str) -> None:
        try:
            participant = self._registry.get(participant_id)
        except NotFound:
            return
        observed = self.snapshot(participant_id).state
        published = self._published_states.get(participant_id, observed)
        if published is not observed and not self._stopping:
            self._bump_revision()
            self._published_states[participant_id] = observed
            self._publish_change(participant_id)
        await self._provider.refresh((participant,))
        if not self._stopping:
            self._bump_revision()
            after = self.snapshot(participant_id).state
            before = self._published_states.get(participant_id, observed)
            self._published_states[participant_id] = after
            if before is not after:
                self._publish_change(participant_id)

    def _publish_change(self, participant_id: str) -> None:
        try:
            if self._on_change is not None:
                self._on_change(participant_id)
        except Exception:
            logger.exception("publishing presence change for %s failed", participant_id)

    def _bump_revision(self) -> None:
        self._revision += 1
        pending = self._revision_event
        self._revision_event = asyncio.Event()
        pending.set()

    async def _loop(self) -> None:
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._refresh_interval)
            self._wake.clear()
            await self.refresh()


__all__ = ["PresenceMonitor"]
