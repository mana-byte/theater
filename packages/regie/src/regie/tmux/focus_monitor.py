"""Bridge-owned focus lifetime, shared fresh reads, and wake invalidation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace

from regie.tmux.focus_facts import FocusInventory, read_inventory
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
        self._stopping = True
        self._reason = "focus_not_observed"
        self._epoch = 0
        self._armed_at = 0.0
        self._refresh_task: asyncio.Task | None = None
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

    async def refresh(self, *, fresh: bool = False) -> None:
        if self._stopping:
            return
        task = self._refresh_task
        if fresh and task is not None and not task.done():
            await asyncio.shield(task)
            if self._stopping:
                return
            task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._read(), name="regie-focus-read")
            self._refresh_task = task
        await asyncio.shield(task)

    async def _read(self) -> None:
        hooks = self._hooks
        if hooks is None:
            return
        epoch = self._epoch
        try:
            async with asyncio.timeout(_READ_TIMEOUT_SECONDS):
                facts = await read_inventory(hooks.identity.value)
        except Exception:
            self._trust.invalidate()
            self._invalidate("focus_query_failed")
            return
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
        if self._facts != facts:
            self._epoch += 1
            self.changed.set()
        self._facts = facts

    async def observe(self, expected: PaneSnapshot) -> PresenceEvidence:
        await self.refresh(fresh=True)
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
        tasks = [task for task in (self._loop_task, self._waiter_task, self._refresh_task) if task]
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
