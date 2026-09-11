"""The one shared human-presence monitor: bounded refresh, wake hooks, snapshots.

Presence is derived facts, never hook payloads: hooks only wake a fresh
inventory, so delayed or repeated hooks cannot inject predecessor state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from theater.constants.presence import (
    PRESENCE_CLOSE_TIMEOUT_SECONDS,
    PRESENCE_REFRESH_INTERVAL_SECONDS,
    PRESENCE_WAKE_BACKOFF_SECONDS,
    PRESENCE_WAKE_CHANNEL,
)
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.models import Busy, HumanPresent

logger = logging.getLogger("theater.daemon.presence")

_FAILING_REASON = "query-failed"
_UNOBSERVED_REASON = "not-observed"
_UNREGISTERED_REASON = "unregistered"


class PresenceMonitor:
    """Implements PresenceProvider for the daemon against one tmux server.

    One monitor owns the periodic refresh, the coalesced wait-for wake, and
    the per-participant snapshot cache; every consumer reads cached facts.
    """

    def __init__(self, registry, *, refresh_interval: float = PRESENCE_REFRESH_INTERVAL_SECONDS):
        self._registry = registry
        self._refresh_interval = refresh_interval
        self._channel = PRESENCE_WAKE_CHANNEL
        self._snapshots: dict[str, PresenceSnapshot] = {}
        self._known_ids: frozenset[str] = frozenset()
        self._revision = 0
        self._revision_event: asyncio.Event = asyncio.Event()
        self._wake: asyncio.Event = asyncio.Event()
        self._refresh_fut: asyncio.Future | None = None
        self._loop_task: asyncio.Task | None = None
        self._waiter_task: asyncio.Task | None = None
        self._rearm_task: asyncio.Task | None = None
        self._identity: str | None = None
        self._observed_at: float | None = None
        self._armed = False
        self._stopping = False

    # ---- PresenceProvider -----------------------------------------------

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def observed_at(self) -> float | None:
        return self._observed_at

    def snapshot(self, participant_id: str) -> PresenceSnapshot:
        """The cached snapshot; synchronous and never queries tmux."""
        cached = self._snapshots.get(participant_id)
        if cached is not None:
            return cached
        known = participant_id in self._known_ids or any(
            p.id == participant_id for p in self._registry.list()
        )
        reason = _UNOBSERVED_REASON if known else _UNREGISTERED_REASON
        return PresenceSnapshot(PresenceState.UNKNOWN, reason, self._revision, self._observed_at)

    async def refresh(self) -> None:
        """One fresh inventory; concurrent callers join the in-flight refresh."""
        existing = self._refresh_fut
        if existing is not None:
            await asyncio.shield(existing)
            return
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._refresh_fut = fut
        try:
            await self._refresh_once()
        finally:
            self._refresh_fut = None
            if not fut.done():
                fut.set_result(None)

    async def require_absent(self, participant_id: str) -> None:
        """Fresh inventory first — control side effects never read a stale cache."""
        await self.refresh()
        snapshot = self.snapshot(participant_id)
        if snapshot.state is PresenceState.PRESENT:
            raise HumanPresent(
                f"a human is present at {participant_id!r} ({snapshot.reason}); not mutating"
            )
        if snapshot.state is PresenceState.UNKNOWN:
            raise Busy(
                f"human presence for {participant_id!r} is unknown "
                f"({snapshot.reason}); not mutating"
            )

    async def wait_for_change(self, after_revision: int) -> int:
        """Wait until the revision moves past ``after_revision``; no missed wakeups."""
        while self._revision <= after_revision:
            event = self._revision_event
            await event.wait()
        return self._revision

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Observe once, then run the loops; arming happened in reconcile."""
        if self._loop_task is not None or self._waiter_task is not None:
            return
        from theater.tmux import presence as tmux_presence

        self._stopping = False
        await self._arm(tmux_presence)
        await self.refresh()
        self._loop_task = asyncio.create_task(self._loop(), name="presence-refresh")
        self._waiter_task = asyncio.create_task(self._waiter_loop(), name="presence-waiter")

    async def reconcile(self) -> None:
        """Re-arm focus events and wake hooks (idempotent), then refresh."""
        from theater.tmux import presence as tmux_presence

        if self._stopping:
            return
        await self._arm(tmux_presence, force=True)
        await self.refresh()

    async def aclose(self) -> None:
        """Cancel and reap owned tasks, sweep owned hooks; leave focus-events on."""
        self._stopping = True
        tasks = [
            task
            for task in (self._loop_task, self._waiter_task, self._rearm_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    PRESENCE_CLOSE_TIMEOUT_SECONDS,
                )
        self._loop_task = self._waiter_task = self._rearm_task = None
        try:
            from theater.tmux import presence as tmux_presence

            await tmux_presence.remove_focus_wake_hooks(self._channel)
        except Exception:
            logger.warning("could not remove presence wake hooks on shutdown", exc_info=True)

    # ---- internals -------------------------------------------------------

    async def _arm(self, tmux_presence, *, force: bool = False) -> None:
        """Enable focus events and install wake hooks; failures only slow us down."""
        if self._armed and not force:
            return
        self._armed = True
        try:
            status = await tmux_presence.ensure_focus_events()
            if status.previously_off:
                logger.warning(
                    "focus-events was off; enabled globally, existing clients read "
                    "present until reattached"
                )
            if status.focusless_clients:
                logger.warning(
                    "attached clients %s cannot report focus; they read as "
                    "present until reattached",
                    ", ".join(status.focusless_clients),
                )
        except Exception:
            logger.warning(
                "could not ensure focus-events; presence stays fail-closed", exc_info=True
            )
        try:
            armed = await tmux_presence.install_focus_wake_hooks(self._channel)
            logger.info("presence wake hooks armed: %d entries", len(armed))
        except Exception:
            logger.warning(
                "could not install presence wake hooks; periodic refresh only",
                exc_info=True,
            )

    async def _refresh_once(self) -> None:
        from theater.tmux import presence as tmux_presence

        try:
            inventory = await tmux_presence.observe_focus_inventory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._publish_failure(exc)
            return
        self._publish(inventory)

    def _publish(self, inventory) -> None:
        revision = self._revision + 1
        snapshots = {
            participant.id: self._derive(participant, inventory, revision)
            for participant in self._registry.list()
        }
        self._snapshots = snapshots
        self._known_ids = frozenset(snapshots)
        self._observed_at = inventory.observed_at
        identity_changed = (
            bool(self._identity)
            and bool(inventory.server_identity)
            and (inventory.server_identity != self._identity)
        )
        if inventory.server_identity:
            self._identity = inventory.server_identity
        self._bump_revision()
        if identity_changed:
            logger.warning("tmux server identity changed; re-arming presence wake hooks")
            self._schedule_rearm()

    def _publish_failure(self, exc: Exception) -> None:
        """Fail closed: every live participant reads UNKNOWN until a query succeeds."""
        revision = self._revision + 1
        reason = f"{_FAILING_REASON}: {type(exc).__name__}"
        snapshots = {
            participant.id: PresenceSnapshot(PresenceState.UNKNOWN, reason, revision, None)
            for participant in self._registry.list()
        }
        self._snapshots = snapshots
        self._known_ids = frozenset(snapshots)
        self._bump_revision()
        logger.warning("presence inventory query failed; all snapshots unknown", exc_info=True)

    def _derive(self, participant, inventory, revision: int) -> PresenceSnapshot:
        """Fold one fresh inventory into one participant's presence snapshot."""
        observed_at = inventory.observed_at
        if not participant.tmux_pane:
            return PresenceSnapshot(PresenceState.ABSENT, "no-pane", revision, observed_at)
        expected = participant.tmux_server_identity
        if not expected:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "identity-unstamped", revision, observed_at
            )
        if expected != inventory.server_identity:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "server-identity-changed", revision, observed_at
            )
        window_id = inventory.panes.get(participant.tmux_pane)
        if window_id is None:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "pane-not-in-inventory", revision, observed_at
            )
        viewers = [
            client
            for client in inventory.clients
            if client.focused and client.input_capable and client.window_id == window_id
        ]
        if not viewers:
            return PresenceSnapshot(
                PresenceState.ABSENT, "no-focused-viewer", revision, observed_at
            )
        if any(client.active_pane_id == participant.tmux_pane for client in viewers):
            return PresenceSnapshot(PresenceState.PRESENT, "focused-viewer", revision, observed_at)
        return PresenceSnapshot(
            PresenceState.UNKNOWN, "independent-active-pane", revision, observed_at
        )

    def _bump_revision(self) -> None:
        """Publish a new revision and release every waiter exactly once."""
        self._revision += 1
        pending = self._revision_event
        self._revision_event = asyncio.Event()
        pending.set()

    def _schedule_rearm(self) -> None:
        if self._rearm_task is not None and not self._rearm_task.done():
            return

        async def _rearm() -> None:
            from theater.tmux import presence as tmux_presence

            await self._arm(tmux_presence, force=True)

        self._rearm_task = asyncio.create_task(_rearm(), name="presence-rearm")

    async def _loop(self) -> None:
        """Periodic refresh plus hook wakes; both funnel into one coalesced refresh."""
        while not self._stopping:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._refresh_interval)
            self._wake.clear()
            with contextlib.suppress(asyncio.CancelledError):
                await self.refresh()
            # A wake that arrived during the refresh is not lost: the event set
            # before clear() ordering guarantees one more pass.
            if self._wake.is_set():
                continue

    async def _waiter_loop(self) -> None:
        """Own the single wait-for client; each wake latches the shared event."""
        from theater.tmux import presence as tmux_presence

        while not self._stopping:
            try:
                await tmux_presence.wait_for_wake(self._channel)
            except asyncio.CancelledError:
                raise
            except Exception:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(PRESENCE_WAKE_BACKOFF_SECONDS)
                continue
            if self._stopping:
                return
            self._wake.set()
