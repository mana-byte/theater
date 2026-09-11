"""The shared human-presence monitor: refresh, wake hooks, trusted snapshots."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from theater.constants.presence import (
    PRESENCE_ARM_CHECK_INTERVAL_SECONDS,
    PRESENCE_CLOSE_TIMEOUT_SECONDS,
    PRESENCE_INVENTORY_STALE_SECONDS,
    PRESENCE_REFRESH_INTERVAL_SECONDS,
    PRESENCE_REFRESH_TIMEOUT_SECONDS,
    PRESENCE_SETTLE_SECONDS,
    PRESENCE_WAKE_BACKOFF_SECONDS,
    PRESENCE_WAKE_CHANNEL,
)
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.models import Busy, HumanPresent

logger = logging.getLogger("theater.daemon.presence")

_FAILING_REASON = "query-failed"
# Actionable guidance attached to every required-UNKNOWN refusal.
_AWAIT_GUIDANCE = "await presence.wait_for_change(revision) or a fresh refresh, then retry"


class PresenceMonitor:
    """Implements PresenceProvider for the daemon against one tmux server.

    Fail-closed: unknown, stale, or rebound facts always protect the pane.
    """

    def __init__(
        self,
        registry,
        *,
        refresh_interval: float = PRESENCE_REFRESH_INTERVAL_SECONDS,
        stale_after: float = PRESENCE_INVENTORY_STALE_SECONDS,
        arm_check_interval: float = PRESENCE_ARM_CHECK_INTERVAL_SECONDS,
        clock=time.time,
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
        self._refresh_task: asyncio.Task | None = None
        self._refresh_error: Exception | None = None
        self._loop_task: asyncio.Task | None = None
        self._waiter_task: asyncio.Task | None = None
        self._rearm_task: asyncio.Task | None = None
        self._identity: str | None = None
        self._observed_at: float | None = None
        # Focus-trust epoch: any option reset or probe failure invalidates it.
        self._epoch = 0
        # client identity -> (last focused literal, epoch of last transition)
        self._focus_evidence: dict[tuple[str, str], tuple[bool, int]] = {}
        self._armed_once = False
        self._arm_ok = False
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
        participant = self._registry.get(participant_id)
        if participant is None:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "unregistered", self._revision, self._observed_at
            )
        if self._stopping:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "monitor-closed", self._revision, self._observed_at
            )
        binding = self._binding_of(participant)
        if participant_id in self._bindings and self._bindings[participant_id] != binding:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "participant-changed", self._revision, self._observed_at
            )
        cached = self._snapshots.get(participant_id)
        if cached is None:
            return PresenceSnapshot(PresenceState.UNKNOWN, "not-observed", self._revision, None)
        if self._observed_at is None:
            # A failed publish: the cached UNKNOWN carries the query failure.
            return cached
        if self._clock() - self._observed_at > self._stale_after:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "stale-inventory", cached.revision, self._observed_at
            )
        return cached

    async def refresh(self) -> None:
        """One fresh inventory; callers join the monitor-owned bounded task."""
        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._refresh_owned(), name="presence-refresh-once")
            self._refresh_task = task
        # Shielded: a cancelled caller abandons, the owned refresh still lands.
        await asyncio.shield(task)

    async def require_absent(self, participant_id: str) -> None:
        """Fresh facts before any control side effect; refusals carry guidance."""
        participant = self._registry.get(participant_id)
        if participant is None or not participant.tmux_pane:
            raise Busy(f"participant {participant_id!r} has no pane to protect")
        await self.refresh()
        if self._refresh_error is not None:
            raise HumanPresent(
                f"human presence for {participant_id!r} is unknown "
                f"({_FAILING_REASON}: {type(self._refresh_error).__name__}); not mutating; "
                f"{_AWAIT_GUIDANCE}"
            )
        snapshot = self.snapshot(participant_id)
        if snapshot.state is not PresenceState.ABSENT:
            raise HumanPresent(
                f"human presence for {participant_id!r} is {snapshot.state.value} "
                f"({snapshot.reason}); not mutating; {_AWAIT_GUIDANCE}"
            )
        from theater.tmux import presence as tmux_presence

        try:
            in_copy_mode = await tmux_presence.human_present(participant.tmux_pane)
        except Exception as exc:
            raise HumanPresent(
                f"human presence for {participant_id!r} is unknown "
                f"(copy-mode query failed: {type(exc).__name__}); not mutating; {_AWAIT_GUIDANCE}"
            ) from exc
        if in_copy_mode:
            raise Busy(
                f"pane {participant.tmux_pane} is in copy mode; "
                f"await presence.wait_for_change({self._revision}) and retry"
            )

    async def wait_for_change(self, after_revision: int) -> int:
        """Wait until the revision moves past ``after_revision``; no missed wakeups."""
        while self._revision <= after_revision:
            event = self._revision_event
            await event.wait()
        return self._revision

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        """Arm, observe once; loops run before the first await parks the waiter."""
        if self._loop_task is not None or self._waiter_task is not None:
            return
        from theater.tmux import presence as tmux_presence

        self._stopping = False
        # Tasks first: the waiter parks while arming and the first refresh
        # yield, so a hook burst during startup is never signalled into void.
        self._loop_task = asyncio.create_task(self._loop(), name="presence-refresh")
        self._waiter_task = asyncio.create_task(self._waiter_loop(), name="presence-waiter")
        await self._arm(tmux_presence)
        await self.refresh()

    async def reconcile(self) -> None:
        """Force an arm pass (option, hooks) and one fresh inventory."""
        from theater.tmux import presence as tmux_presence

        if self._stopping:
            return
        await self._arm(tmux_presence, force=True)
        await self.refresh()

    async def aclose(self) -> None:
        """Cancel and reap every owned task and hook sweep, under one bound."""
        self._stopping = True
        tasks = [
            task
            for task in (
                self._loop_task,
                self._waiter_task,
                self._rearm_task,
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
        self._loop_task = self._waiter_task = self._rearm_task = self._refresh_task = None

    # ---- internals -------------------------------------------------------

    async def _refresh_owned(self) -> None:
        """One bounded observation owned by the monitor, never by a caller."""
        from theater.tmux import presence as tmux_presence

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
        self._publish(inventory)

    def _binding_of(self, participant) -> tuple:
        return (participant.tmux_pane, participant.tmux_server_identity, participant.pid)

    async def _arm(self, tmux_presence, *, force: bool = False) -> None:
        """Ensure the option and hook coverage; a failure invalidates trust."""
        due = self._last_arm_at is None or (
            self._clock() - self._last_arm_at >= self._arm_check_interval
        )
        if self._armed_once and not force and not due:
            return
        self._armed_once = True
        try:
            status = await tmux_presence.ensure_focus_events()
        except Exception:
            # Unverifiable flags protect: every blur read stays UNKNOWN.
            self._epoch += 1
            self._arm_ok = False
            logger.warning(
                "could not ensure focus-events; presence stays fail-closed", exc_info=True
            )
        else:
            self._arm_ok = True
            if status.previously_off:
                # Flags frozen during the off epoch cannot prove absence.
                self._epoch += 1
                logger.warning("focus-events was off; enabled, blur evidence invalidated")
            if status.focusless_clients:
                logger.warning(
                    "attached clients %s cannot report focus; they read as present",
                    ", ".join(status.focusless_clients),
                )
        try:
            armed = await tmux_presence.install_focus_wake_hooks(self._channel)
            logger.info("presence wake hooks armed: %d entries", len(armed))
        except Exception:
            logger.warning(
                "could not install presence wake hooks; periodic refresh only",
                exc_info=True,
            )
        self._last_arm_at = self._clock()

    def _publish(self, inventory) -> None:
        identity_changed = (
            bool(self._identity)
            and bool(inventory.server_identity)
            and inventory.server_identity != self._identity
        )
        if identity_changed:
            # No client survives a restart, so no evidence survives either.
            self._epoch += 1
            self._focus_evidence.clear()
        self._update_focus_evidence(inventory)
        revision = self._revision + 1
        participants = self._registry.list()
        self._snapshots = {
            participant.id: self._derive(participant, inventory, revision)
            for participant in participants
        }
        self._bindings = {p.id: self._binding_of(p) for p in participants}
        self._observed_at = inventory.observed_at
        if inventory.server_identity:
            self._identity = inventory.server_identity
        self._bump_revision()
        if identity_changed:
            logger.warning("tmux server identity changed; re-arming presence wake hooks")
            self._schedule_rearm()

    def _update_focus_evidence(self, inventory) -> None:
        """Track per-client focus transitions; trust needs a same-epoch flip."""
        seen = set()
        for client in inventory.clients:
            key = client.identity
            seen.add(key)
            previous = self._focus_evidence.get(key)
            if previous is None:
                # CLIENT_FOCUSED defaults on: a first sighting is not evidence.
                self._focus_evidence[key] = (client.focused, -1)
            else:
                prev_focused, trusted_epoch = previous
                if client.focused != prev_focused:
                    trusted_epoch = self._epoch
                self._focus_evidence[key] = (client.focused, trusted_epoch)
        for gone in set(self._focus_evidence) - seen:
            del self._focus_evidence[gone]

    def _trusted(self, client) -> bool:
        evidence = self._focus_evidence.get(client.identity)
        return evidence is not None and evidence[1] == self._epoch

    def _publish_failure(self, exc: Exception) -> None:
        """Fail closed: every live participant reads UNKNOWN until success."""
        revision = self._revision + 1
        reason = f"{_FAILING_REASON}: {type(exc).__name__}"
        participants = self._registry.list()
        self._snapshots = {
            participant.id: PresenceSnapshot(PresenceState.UNKNOWN, reason, revision, None)
            for participant in participants
        }
        self._bindings = {p.id: self._binding_of(p) for p in participants}
        self._observed_at = None
        self._bump_revision()
        logger.warning("presence inventory query failed; all snapshots unknown", exc_info=True)

    def _derive(self, participant, inventory, revision: int) -> PresenceSnapshot:
        """Fold one fresh inventory into one participant's presence facts."""
        observed_at = inventory.observed_at
        guard = self._binding_guard(participant, inventory, revision, observed_at)
        if guard is not None:
            return guard
        window_id = inventory.panes[participant.tmux_pane]
        present = unknown_blur = unknown_selection = released = False
        for client in inventory.clients:
            verdict = self._client_verdict(client, participant.tmux_pane, window_id, inventory)
            if verdict == "present":
                present = True
            elif verdict == "unknown-blur":
                unknown_blur = True
            elif verdict == "unknown-selection":
                unknown_selection = True
            elif verdict == "released":
                released = True
        if present:
            return PresenceSnapshot(PresenceState.PRESENT, "focused-viewer", revision, observed_at)
        if unknown_blur:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "focus-unverified", revision, observed_at
            )
        if unknown_selection:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "independent-active-pane", revision, observed_at
            )
        if released:
            return PresenceSnapshot(PresenceState.ABSENT, "pane-released", revision, observed_at)
        return PresenceSnapshot(PresenceState.ABSENT, "no-viewer", revision, observed_at)

    def _binding_guard(self, participant, inventory, revision, observed_at):
        """UNKNOWN guards that precede any viewer classification."""
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
        if participant.tmux_pane not in inventory.panes:
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "pane-not-in-inventory", revision, observed_at
            )
        if participant.pid is not None and inventory.pane_pids.get(participant.tmux_pane) != str(
            participant.pid
        ):
            return PresenceSnapshot(
                PresenceState.UNKNOWN, "pane-pid-changed", revision, observed_at
            )
        return None

    def _client_verdict(self, client, pane_id, window_id, inventory) -> str | None:
        """One client's contribution: present, unknown*, released, or None."""
        if not client.input_capable or client.window_id != window_id:
            return None
        if client.active_pane_id == pane_id:
            if client.focused:
                return "present"
            # A blur literal without a same-epoch transition observed proves
            # nothing: the flag may be frozen or uninitialized.
            return "released" if self._trusted(client) else "unknown-blur"
        if inventory.panes.get(client.active_pane_id) == window_id:
            # A regular pane selection releases the participant's pane.
            return "released"
        # Selection unobservable: the client's active pane is missing or
        # outside the window it is reported to be viewing.
        return "unknown-selection"

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
                self._schedule_rearm()
            with contextlib.suppress(asyncio.CancelledError):
                await self.refresh()
                if signalled and not self._stopping:
                    # A hook burst fires several signals inside one re-park
                    # gap; the first refresh can land mid-transition.
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
            self._wake.set()
