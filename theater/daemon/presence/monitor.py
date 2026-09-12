"""The shared human-presence monitor: refresh ownership, arming, snapshots."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from theater.constants.presence import (
    PRESENCE_ARM_CHECK_INTERVAL_SECONDS,
    PRESENCE_ARM_TIMEOUT_SECONDS,
    PRESENCE_CLOSE_TIMEOUT_SECONDS,
    PRESENCE_INVENTORY_STALE_SECONDS,
    PRESENCE_REFRESH_INTERVAL_SECONDS,
    PRESENCE_REFRESH_TIMEOUT_SECONDS,
    PRESENCE_SETTLE_SECONDS,
    PRESENCE_WAKE_BACKOFF_SECONDS,
    PRESENCE_WAKE_CHANNEL,
)
from theater.daemon.presence.classify import FocusTrust, derive
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.models import HumanPresent, NotFound, Status

logger = logging.getLogger("theater.daemon.presence")

_FAILING_REASON = "query-failed"
# Actionable guidance attached to every required-UNKNOWN refusal.
_AWAIT_GUIDANCE = "call await_sessions(handles=[{participant_id!r}]), then retry"

#: UNKNOWN reasons that describe wake churn rather than a verdict about the
#: pane: a wake landed inside the inventory read (a torn observation) or the
#: waiter has not refreshed after a hook burst yet. Both settle within one
#: owned refresh — daemon-caused topology (kills, spawns) trips them routinely
#: — while every other UNKNOWN is a stable classification.
_REASON_TORN_READ = "focus-changed-during-query"
_REASON_REFRESH_PENDING = "focus-refresh-pending"
_TRANSIENT_UNKNOWN_REASONS = frozenset((_REASON_TORN_READ, _REASON_REFRESH_PENDING))


class PresenceMonitor:
    """Focus-only PresenceProvider; fail-closed snapshots and owned arming."""

    def __init__(
        self,
        registry,
        *,
        refresh_interval: float = PRESENCE_REFRESH_INTERVAL_SECONDS,
        stale_after: float = PRESENCE_INVENTORY_STALE_SECONDS,
        arm_check_interval: float = PRESENCE_ARM_CHECK_INTERVAL_SECONDS,
        clock=time.monotonic,
    ):
        self._registry = registry
        self._refresh_interval = refresh_interval
        self._stale_after = stale_after
        self._arm_check_interval = arm_check_interval
        self._clock = clock
        self._channel = PRESENCE_WAKE_CHANNEL
        self._snapshots: dict[str, PresenceSnapshot] = {}
        self._bindings: dict[str, tuple] = {}
        self._revision = 0
        self._revision_event: asyncio.Event = asyncio.Event()
        self._wake: asyncio.Event = asyncio.Event()
        self._wake_epoch = 0
        self._refresh_task: asyncio.Task | None = None
        self._refresh_error: Exception | None = None
        self._loop_task: asyncio.Task | None = None
        self._waiter_task: asyncio.Task | None = None
        self._arm_task: asyncio.Task | None = None
        self._last_inventory = None
        self._identity: str | None = None
        self._observed_at: float | None = None
        self._observed_mono: float | None = None
        self._trust = FocusTrust()
        self._armed_once = False
        self._last_arm_at: float | None = None
        self._stopping = False

    # ---- PresenceProvider -----------------------------------------------

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def observed_at(self) -> float | None:
        return self._observed_at

    def snapshot(self, participant_id: str) -> PresenceSnapshot:
        """Cached facts, synchronous; stale or rebound facts fail closed."""
        if self._stopping:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "monitor-closed", self._revision, self._observed_at
            )
        try:
            participant = self._registry.get(participant_id)
        except NotFound:
            participant = None
        if participant is None or not participant.tmux_pane:
            return PresenceSnapshot(
                PresenceState.ABSENT,
                "unregistered" if participant is None else "no-pane",
                self._revision,
                self._observed_at,
            )
        binding = self._binding_of(participant)
        if participant_id in self._bindings and self._bindings[participant_id] != binding:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "participant-changed", self._revision, self._observed_at
            )
        cached = self._snapshots.get(participant_id)
        if self._observed_mono is None:
            return cached or PresenceSnapshot(
                PresenceState.UNKNOWN, "not-observed", self._revision, None
            )
        if self._clock() - self._observed_mono > self._stale_after:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "stale-inventory", self._revision, self._observed_at
            )
        if cached is not None and participant.status is not Status.DEAD:
            return cached
        return derive(participant, self._last_inventory, self._revision, self._trust)

    async def refresh(self) -> None:
        """One fresh inventory; callers join the monitor-owned bounded task."""
        if self._stopping:
            return
        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._refresh_owned(), name="presence-refresh-once")
            self._refresh_task = task
        # Shielded: a cancelled caller abandons, the owned refresh still lands.
        await asyncio.shield(task)

    async def require_absent(self, participant_id: str) -> None:
        """Fresh facts before any control side effect; refusals carry guidance."""
        try:
            participant = self._registry.get(participant_id)
        except NotFound:
            participant = None
        if not self._stopping and (participant is None or not participant.tmux_pane):
            # No pane, nothing to protect; addressability is a control-gate fact.
            return
        guidance = _AWAIT_GUIDANCE.format(participant_id=participant_id)
        snapshot = await self._fresh_snapshot(participant_id, guidance)
        if (
            snapshot.state is PresenceState.UNKNOWN
            and snapshot.reason in _TRANSIENT_UNKNOWN_REASONS
        ):
            # Churn, not a verdict: the daemon's own topology mutations tear
            # the very inventory this gate reads (a kill fires after-kill-pane
            # and window-unlinked wakes; a spawn fires after-new-window), and
            # a wake landing mid-read discards the observation. One
            # settle-and-refresh turns it back into the coherent answer the
            # gate exists to enforce; any other UNKNOWN refuses immediately,
            # and a settled PRESENT still refuses.
            await asyncio.sleep(PRESENCE_SETTLE_SECONDS)
            snapshot = await self._fresh_snapshot(participant_id, guidance)
        if snapshot.state is not PresenceState.ABSENT:
            raise HumanPresent(
                f"human presence for {participant_id!r} is {snapshot.state.value} "
                f"({snapshot.reason}); not mutating; {guidance}"
            )

    async def _fresh_snapshot(self, participant_id: str, guidance: str) -> PresenceSnapshot:
        """One owned refresh, then the snapshot; a failed refresh refuses."""
        await self.refresh()
        if self._refresh_error is not None:
            raise HumanPresent(
                f"human presence for {participant_id!r} is unknown "
                f"({_FAILING_REASON}: {type(self._refresh_error).__name__}); not mutating; "
                f"{guidance}"
            )
        return self.snapshot(participant_id)

    async def wait_for_change(self, after_revision: int) -> int:
        """Wait until the revision moves past ``after_revision``; no missed wakeups."""
        while self._revision <= after_revision:
            event = self._revision_event
            await event.wait()
        return self._revision

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Create loops first, then join the owned arm and first refresh."""
        if self._loop_task is not None or self._waiter_task is not None:
            return
        self._stopping = False
        # Tasks first: the waiter parks while arming and the first refresh
        # yield, so a hook burst during startup is never signalled into void.
        self._loop_task = asyncio.create_task(self._loop(), name="presence-refresh")
        self._waiter_task = asyncio.create_task(self._waiter_loop(), name="presence-waiter")
        await self._arm()
        await self.refresh()

    async def reconcile(self) -> None:
        """Force an arm pass (option, hooks) and one fresh inventory."""
        if self._stopping:
            return
        await self._arm(force=True)
        await self.refresh()

    async def aclose(self) -> None:
        """Cancel and reap every owned task and hook sweep, under one bound."""
        self._stopping = True
        tasks = [
            task
            for task in (
                self._loop_task,
                self._waiter_task,
                self._arm_task,
                self._refresh_task,
            )
            if task is not None
        ]
        try:
            async with asyncio.timeout(PRESENCE_CLOSE_TIMEOUT_SECONDS):
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                from theater.tmux import presence as tmux_presence

                await tmux_presence.remove_focus_wake_hooks(self._channel)
        except TimeoutError:
            logger.warning("presence close timed out; wake hooks may linger")
        self._loop_task = self._waiter_task = self._arm_task = self._refresh_task = None

    # ---- internals -------------------------------------------------------

    async def _refresh_owned(self) -> None:
        """One bounded observation owned by the monitor, never by a caller."""
        from theater.tmux import presence as tmux_presence

        if self._stopping:
            return
        observed_mono = self._clock()
        wake_epoch = self._wake_epoch
        try:
            async with asyncio.timeout(PRESENCE_REFRESH_TIMEOUT_SECONDS):
                inventory = await tmux_presence.observe_focus_inventory()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._refresh_error = exc
            self._publish_failure(exc)
            return
        if self._stopping:
            return
        self._refresh_error = None
        if wake_epoch != self._wake_epoch:
            self._publish_unknown(_REASON_TORN_READ)
            self._wake.set()
            return
        self._publish(inventory, observed_mono=observed_mono)

    def _binding_of(self, participant) -> tuple:
        return (participant.tmux_pane, participant.tmux_server_identity, participant.pid)

    async def _arm(self, *, force: bool = False) -> None:
        """Join the one owned bounded arm pass; cancellation leaves it owned."""
        await asyncio.shield(self._ensure_arm_task(force=force))

    def _ensure_arm_task(self, *, force: bool) -> asyncio.Task:
        # One arm pass in flight at a time: fresh requests coalesce into it.
        task = self._arm_task
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(self._arm_owned(force=force), name="presence-arm")
        self._arm_task = task
        return task

    async def _arm_owned(self, *, force: bool = False) -> None:
        """Bounded arm pass owned by the monitor; invalidates trust on failure."""
        from theater.tmux import presence as tmux_presence

        due = self._last_arm_at is None or (
            self._clock() - self._last_arm_at >= self._arm_check_interval
        )
        if self._armed_once and not force and not due:
            return
        self._armed_once = True
        if self._stopping:
            return
        self._trust.arm_ok = False
        self._rederive()
        try:
            async with asyncio.timeout(PRESENCE_ARM_TIMEOUT_SECONDS):
                status = await tmux_presence.ensure_focus_events()
                if self._stopping:
                    return
                if not status.enabled or status.previously_off:
                    self._trust.invalidate()
                armed = await tmux_presence.install_focus_wake_hooks(self._channel)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unverifiable flags protect: every blur read stays UNKNOWN.
            self._trust.arm_ok = False
            self._trust.invalidate()
            logger.warning(
                "could not ensure focus-events; presence stays fail-closed", exc_info=True
            )
        else:
            self._trust.arm_ok = status.enabled
            if not status.enabled:
                self._trust.invalidate()
                logger.warning("focus-events could not be verified on; trust invalidated")
            if status.previously_off:
                # Flags frozen during the off epoch cannot prove absence.
                self._trust.invalidate()
                logger.warning("focus-events was off; blur evidence invalidated")
            if status.focusless_clients:
                logger.warning(
                    "attached clients %s cannot report focus; they read as present",
                    ", ".join(status.focusless_clients),
                )
            logger.info("presence wake hooks armed: %d entries", len(armed))
        finally:
            self._last_arm_at = self._clock()
        self._rederive()

    def _publish(self, inventory, *, observed_mono: float | None = None) -> None:
        """Apply validity invalidations, then derive fresh snapshots."""
        self._last_inventory = inventory
        identity_changed = (
            bool(self._identity)
            and bool(inventory.server_identity)
            and inventory.server_identity != self._identity
        )
        if identity_changed:
            # No client survives a restart, so no evidence survives either.
            self._trust.invalidate()
            logger.warning("tmux server identity changed; re-arming presence wake hooks")
            self._ensure_arm_task(force=True)
        if not inventory.focus_events_enabled:
            # Flags read while the option is off cannot prove absence.
            self._trust.invalidate()
            self._trust.arm_ok = False
            self._ensure_arm_task(force=True)
            logger.warning("focus-events is off; blur evidence invalidated")
        self._trust.observe(inventory.clients)
        revision = self._revision + 1
        participants = self._registry.list()
        self._snapshots = {
            participant.id: derive(participant, inventory, revision, self._trust)
            for participant in participants
        }
        self._bindings = {p.id: self._binding_of(p) for p in participants}
        self._observed_at = inventory.observed_at
        self._observed_mono = self._clock() if observed_mono is None else observed_mono
        if inventory.server_identity:
            self._identity = inventory.server_identity
        self._bump_revision()

    def _rederive(self) -> None:
        """Re-classify the last inventory under current trust; bumps revision."""
        inventory = self._last_inventory
        if inventory is None or self._observed_mono is None or self._stopping:
            return
        revision = self._revision + 1
        self._snapshots = {
            participant.id: derive(participant, inventory, revision, self._trust)
            for participant in self._registry.list()
        }
        self._bump_revision()

    def _publish_failure(self, exc: Exception) -> None:
        """Fail closed: every live participant reads UNKNOWN until success."""
        self._trust.invalidate()
        self._publish_unknown(f"{_FAILING_REASON}: {type(exc).__name__}")
        logger.warning("presence inventory query failed; all snapshots unknown", exc_info=True)

    def _publish_unknown(self, reason: str) -> None:
        """Invalidate observation facts without replaying hook direction or losing blur trust."""
        self._last_inventory = None
        revision = self._revision + 1
        participants = self._registry.list()
        self._snapshots = {
            participant.id: PresenceSnapshot(PresenceState.UNKNOWN, reason, revision, None)
            for participant in participants
        }
        self._bindings = {p.id: self._binding_of(p) for p in participants}
        self._observed_at = None
        self._observed_mono = None
        self._bump_revision()

    def _bump_revision(self) -> None:
        """Publish a new revision and release every waiter exactly once."""
        self._revision += 1
        pending = self._revision_event
        self._revision_event = asyncio.Event()
        pending.set()

    async def _loop(self) -> None:
        """Periodic refresh, hook wakes, and periodic arm reconciliation."""
        while not self._stopping:
            timeout = min(self._refresh_interval, self._arm_check_interval)
            signalled = True
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
            except TimeoutError:
                signalled = False
            self._wake.clear()
            if not self._stopping and (
                self._last_arm_at is None
                or self._clock() - self._last_arm_at >= self._arm_check_interval
            ):
                self._ensure_arm_task(force=True)
            await self.refresh()
            if signalled and not self._stopping:
                # A hook burst fires several signals inside one re-park gap;
                # the first refresh can land mid-transition.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), PRESENCE_SETTLE_SECONDS)
                self._wake.clear()
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
                # Nonzero exit or tmux error: back off, never busy-spin.
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(PRESENCE_WAKE_BACKOFF_SECONDS)
                continue
            if self._stopping:
                return
            self._wake_epoch += 1
            self._publish_unknown(_REASON_REFRESH_PENDING)
            self._wake.set()
