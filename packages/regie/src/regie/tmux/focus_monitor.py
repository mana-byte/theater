"""Bridge-owned focus lifetime, shared fresh reads, and wake invalidation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace

from regie.tmux.focus_facts import FocusInventory, changed_panes, read_inventory
from regie.tmux.focus_hooks import FocusHooks
from regie.tmux.focus_policy import FocusTrust, PresenceEvidence, classify
from regie.tmux.identity import PaneSnapshot
from regie.tmux.presence import PresenceChanged

logger = logging.getLogger("regie.tmux.presence")
_REFRESH_SECONDS = 2.0
_ARM_SECONDS = 10.0
_READ_TIMEOUT_SECONDS = 5.0


class FocusMonitor:
    def __init__(self) -> None:
        self.changed = asyncio.Event()
        self._wake = asyncio.Event()
        self._trust = FocusTrust()
        self._hooks: FocusHooks | None = None
        self._facts: FocusInventory | None = None
        self._blurred: frozenset[tuple[str, ...]] = frozenset()
        self._pending: frozenset[str] | None = None  # None: scope unknown, report wholesale
        self._stopping = True
        self._reason = "focus_not_observed"
        self._epoch = 0
        self._armed_at = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._reading: set[asyncio.Task] = set()
        self._reads = self._task_read = self._started_read = self._installed_read = 0
        self._read_started_at = self._facts_started_at = 0.0
        self._loop_task: asyncio.Task | None = None
        self._waiter_task: asyncio.Task | None = None

    async def start(self, server_identity: str) -> None:
        if self._hooks is not None and self._hooks.identity.value == server_identity:
            return
        await self.aclose()
        self._stopping = False
        self._hooks = FocusHooks(server_identity)
        self._waiter_task = asyncio.create_task(self._waiter(), name="regie-focus-wakes")
        await self._arm()
        await self.refresh()
        self._loop_task = asyncio.create_task(self._loop(), name="regie-focus-refresh")

    def _invalidate(self, reason: str) -> None:
        changed = self._facts is not None or self._reason != reason
        self._epoch += 1
        self._facts = None
        self._pending = None
        self._reason = reason
        if changed:
            self.changed.set()

    async def _arm(self) -> None:
        assert self._hooks is not None
        try:
            async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                previously_enabled = await self._hooks.arm()
            if not previously_enabled:
                self._trust.invalidate()
                self._invalidate("focus_reporting_rearmed")
                logger.info("enabled tmux focus reporting; attached clients may need reattachment")
            self._trust.armed = True
        except Exception:
            self._trust.armed = False
            self._trust.invalidate()
            self._invalidate("focus_arming_failed")
            logger.warning("could not arm focus reporting; blur remains untrusted", exc_info=True)
        self._armed_at = time.monotonic()

    async def refresh(self, *, fresh: bool = False, requested_at: float | None = None) -> None:
        await self._refresh_once(fresh, requested_at)
        if (
            requested_at is not None
            and self._facts is not None
            and self._facts_started_at < requested_at
        ):
            # An older read installed first and discarded the one we joined: read once more.
            await self._refresh_once(fresh, requested_at)

    async def _refresh_once(self, fresh: bool, requested_at: float | None) -> None:
        if self._stopping:
            return
        if (
            requested_at is not None
            and self._facts is not None
            and self._facts_started_at >= requested_at
        ):
            return  # a read begun after the request arrived is as fresh as one begun now
        task = self._refresh_task
        # A fresh caller may join only a read that has not begun querying tmux, or one that began
        # after its request arrived; otherwise it starts one now instead of queueing behind it.
        began_after = requested_at is not None and self._read_started_at >= requested_at
        if (
            task is None
            or task.done()
            or (fresh and self._started_read >= self._task_read and not began_after)
        ):
            self._reads += 1
            self._task_read = self._reads
            task = asyncio.create_task(self._read(self._reads), name="regie-focus-read")
            self._reading.add(task)
            task.add_done_callback(self._reading.discard)
            self._refresh_task = task
        await asyncio.shield(task)

    async def _read(self, sequence: int) -> None:
        hooks = self._hooks
        if hooks is None:
            return
        epoch = self._epoch
        self._started_read = sequence
        started_at = self._read_started_at = time.monotonic()
        try:
            async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                facts = await read_inventory(hooks.identity.value)
        except Exception:
            if sequence < self._installed_read:
                return
            self._trust.invalidate()
            self._invalidate("focus_query_failed")
            return
        if sequence < self._installed_read:
            return  # a newer concurrent read already installed its evidence
        self._installed_read = sequence
        if epoch != self._epoch:
            self._wake.set()
            return
        if not facts.enabled:
            if self._trust.armed:
                self._armed_at = 0.0
                self._wake.set()
            self._trust.armed = False
            self._trust.invalidate()
        self._trust.observe(facts.clients)
        blurred = frozenset(c.identity for c in facts.clients if self._trust.blurred(c))
        self._note_scope(facts, blurred)
        if self._facts != facts:
            self._epoch += 1
            self.changed.set()
        self._facts = facts
        self._blurred = blurred
        self._facts_started_at = started_at

    def _note_scope(self, facts: FocusInventory, blurred: frozenset[tuple[str, ...]]) -> None:
        """Accumulate panes changed since the last report; no baseline means unknown."""
        if self._pending is None:
            return
        old = self._facts
        scope = None if old is None else changed_panes(old, self._blurred, facts, blurred)
        self._pending = None if scope is None else self._pending | scope

    def take_changed_panes(self) -> frozenset[str] | None:
        """Consume the panes changed since the last take; None means wholesale (unknown)."""
        pending, self._pending = self._pending, frozenset()
        return pending

    async def observe(
        self, expected: PaneSnapshot, *, requested_at: float | None = None
    ) -> PresenceEvidence:
        await self.refresh(fresh=True, requested_at=requested_at)
        if self._facts is None:
            return PresenceEvidence("unknown", self._reason, None)
        return replace(classify(expected, self._facts, self._trust), epoch=self._epoch)

    def validate(self, evidence: PresenceEvidence) -> None:
        """Fence input against focus changes after its fresh admission read."""
        if self._facts is None or evidence.epoch != self._epoch:
            raise PresenceChanged("focus evidence changed before terminal mutation; inspect again")

    async def _loop(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), _REFRESH_SECONDS)
            self._wake.clear()
            if time.monotonic() - self._armed_at >= _ARM_SECONDS:
                await self._arm()
            await self.refresh()

    async def _waiter(self) -> None:
        assert self._hooks is not None
        while True:
            try:
                await self._hooks.wait()
            except Exception:
                self._trust.armed = False
                self._trust.invalidate()
                self._invalidate("focus_wake_unavailable")
                self._wake.set()
                await asyncio.sleep(1.0)
            else:
                self._invalidate("focus_refresh_pending")
                self._wake.set()

    async def aclose(self) -> None:
        self._stopping = True
        self._trust.armed = False
        self._trust.invalidate()
        self._invalidate("focus_monitor_closed")
        tasks = [task for task in (self._loop_task, self._waiter_task) if task]
        tasks += self._reading
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._loop_task = self._waiter_task = self._refresh_task = None
        self._wake.clear()
        hooks, self._hooks = self._hooks, None
        if hooks is not None:
            try:
                async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                    await hooks.close()
            except Exception:
                logger.debug("could not remove owned focus hooks", exc_info=True)
