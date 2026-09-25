"""Watch-task supervision: start/stop, reconcile, and restart one watch per participant."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from theater import timing
from theater.daemon.observation.attachment import AttachmentManager
from theater.daemon.observation.failures import FailureTracker
from theater.daemon.observation.live import EvidenceSink, LiveObservationHub
from theater.daemon.observation.process import ObservationProcess, observation_process
from theater.daemon.registry import Registry
from theater.harness import Harness, HarnessObserver
from theater.harness import normalize as normalize_harness
from theater.harness.channels.hooks import HookRuntime
from theater.harness.channels.otel import NativeOtelRuntime
from theater.harness.channels.wakeup import WakeupSignal
from theater.harness.contracts.channels import ChannelHealth
from theater.harness.contracts.runtime import NativeTurnOutcome
from theater.harness.source import Source
from theater.models import Status, Tier
from theater.observability.catalog import OBSERVER_RESTART, OBSERVER_WATCH

if TYPE_CHECKING:
    from theater.daemon.store import Store

logger = logging.getLogger("theater.observer")


class WatchSupervision:
    if TYPE_CHECKING:
        _attachments: AttachmentManager
        _channel_health: dict[str, tuple[ChannelHealth, ...]]
        _failures: FailureTracker
        harnesses: dict[str, Harness]
        hook_runtime: HookRuntime | None
        live: LiveObservationHub
        otel_runtime: NativeOtelRuntime | None
        _pending_evidence: dict[
            str,
            OrderedDict[
                tuple[int, str, str], tuple[EvidenceSink | None, Source, NativeTurnOutcome]
            ],
        ]
        _readiness_since: float
        registry: Registry
        _restart_pending: set[str]
        _restarts: set[asyncio.Task[None]]
        _retired: set[str]
        _source_processes: dict[str, ObservationProcess | None]
        _stopping: asyncio.Event
        store: Store
        _supervisor: asyncio.Task | None
        sync: float
        _tasks: dict[str, asyncio.Task]
        _unobservable: set[str]
        _clear_primary_channel_health: Callable[..., Any]
        _flush_pending_evidence: Callable[..., Any]
        _restore_transcript_identity_loss: Callable[..., Any]
        _watch: Callable[..., Any]
        _watch_screen: Callable[..., Any]

    def start(self) -> None:
        if not self.harnesses:
            logger.debug("no harnesses configured; observation disabled")
            return
        self._reconcile()
        self._supervisor = asyncio.create_task(self._supervise())

    async def aclose(self) -> None:
        self._stopping.set()
        tasks = list(self._tasks.values())
        tasks.extend(self._restarts)
        if self._supervisor:
            tasks.append(self._supervisor)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._restarts.clear()
        self._restart_pending.clear()
        # Retained evidence only exists between a failed routing and its
        # retry; shutdown ends the retry loop, so the in-memory set goes too.
        self._pending_evidence.clear()
        self._supervisor = None

    async def reset_for_operator_bind(self, pid: str) -> None:
        task = self._tasks.pop(pid, None)
        self._retired.discard(pid)
        self._channel_health.pop(pid, None)
        self._clear_primary_channel_health(pid)
        self._attachments._reset_watch_state.discard(pid)
        self._failures.clear_source_errors(pid, include_identity_lost=True)
        self._failures._identity_loss_replayed.discard(pid)
        if task is not None:
            task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await task

    async def _sleep(self, seconds: float, wake: WakeupSignal | None = None) -> None:
        """Sleep until the interval elapses, the daemon stops, or live data wakes.

        Live wiring only ends the sleep early; the poll interval always remains the fallback.
        """
        if wake is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
            return
        await wake.sleep_until(self._stopping, timeout=seconds)

    async def _supervise(self) -> None:
        while not self._stopping.is_set():
            try:
                self._reconcile()
            except Exception:
                logger.exception("observer reconcile failed")
            await self._sleep(self.sync)

    def _reconcile(self) -> None:
        live = {p.id: p for p in self.registry.list()}
        for pid, task in list(self._tasks.items()):
            if pid not in live:
                self._on_live_change(pid)
                continue
            if pid in live and not task.done():
                if pid in self._source_processes:
                    process = observation_process(self.store, live[pid])
                    if process != self._source_processes[pid]:
                        self._on_live_change(pid)
                continue
            self._tasks.pop(pid)
            task.cancel()
            if pid in live:
                self._retired.add(pid)
                logger.warning("observer for %s stopped; not restarting", pid)
        for pid in live:
            if pid not in self._restart_pending:
                self._start_watch(pid)
        for pid in tuple(self._pending_evidence):
            if pid not in self._tasks:
                self._on_live_change(pid)

    def _start_watch(self, pid: str, *, restarting: bool = False) -> None:
        """Start one participant's watch task if it should have one.

        A registered live channel counts: it carries authoritative status and exact terminal
        evidence even before the durable transcript attaches.
        """
        if self._stopping.is_set() or pid in self._tasks or pid in self._retired:
            return
        p = self.store.get_participant(pid)
        if p is None or p.status is Status.DEAD:
            return
        harness = self.harnesses.get(normalize_harness(p.harness))
        if harness is None:
            self._warn_unobservable(pid, p)
            return
        observer = harness.observer
        hook_active = self._has_active_hooks(p.id, observer)
        otel_active = self._has_active_otel(p.id, observer)
        live_active = self.live.registration_for(p.id) is not None
        durable_source = observer.has_transcript and p.cwd is not None
        provider_bound = self._has_provider_binding(pid)
        if (
            observer.has_transcript
            and not p.cwd
            and not hook_active
            and not otel_active
            and not live_active
            and not provider_bound
        ):
            self._warn_unobservable(pid, p)
            return
        if p.tier is Tier.SPAWNED and not provider_bound and not live_active:
            return
        self._unobservable.discard(pid)
        active_source = durable_source or hook_active or otel_active or live_active
        watch = self._watch if active_source else self._watch_screen
        if durable_source:
            self._restore_transcript_identity_loss(pid)
        if not restarting and p.created_at >= self._readiness_since:
            timing.ready_lag(OBSERVER_WATCH, pid, p.created_at, harness=p.harness)
        self._tasks[pid] = asyncio.create_task(watch(pid, normalize_harness(p.harness)))

    def _on_live_change(self, participant_id: str) -> None:
        """Process or live-channel wiring changed: recompose the participant's watch."""
        if self._stopping.is_set():
            return
        if participant_id in self._restart_pending:
            return
        self._restart_pending.add(participant_id)
        task = asyncio.create_task(self._restart_watch(participant_id))
        self._restarts.add(task)
        task.add_done_callback(self._restarts.discard)

    def _has_provider_binding(self, participant_id: str) -> bool:
        repository = getattr(self.store, "terminal_bindings", None)
        if repository is None:
            return False
        try:
            return repository.get(participant_id) is not None
        except Exception:
            logger.warning(
                "terminal binding lookup failed for %s; preserving its observer",
                participant_id,
                exc_info=True,
            )
            return True

    async def _restart_watch(self, participant_id: str) -> None:
        """Rebuild one watch task around the current effective wiring.

        Composition is fixed at watch start, so a new registration restarts it; awaiting the
        cancelled task keeps its cleanup from racing the new watcher.
        """
        restarting = participant_id in self._tasks
        measurement = (
            timing.span(OBSERVER_RESTART, id=participant_id)
            if restarting
            else contextlib.nullcontext()
        )
        try:
            with measurement:
                task = self._tasks.get(participant_id)
                if task is not None:
                    self._tasks.pop(participant_id, None)
                    task.cancel()
                    with contextlib.suppress(Exception, asyncio.CancelledError):
                        await task
                await self._flush_pending_evidence(participant_id)
                self._start_watch(participant_id, restarting=restarting)
        finally:
            # Keep the participant pending through old-watch cleanup and the
            # replacement start. Any number of intervening registration
            # changes coalesce into one rebuild, which reads the latest hub
            # registration when the new watch actually opens its source.
            self._restart_pending.discard(participant_id)

    def _has_active_hooks(self, participant_id: str, observer: HarnessObserver) -> bool:
        if self.hook_runtime is None:
            return False
        return self.hook_runtime.has_active(participant_id, observer.enrichment_manifests())

    def _has_active_otel(self, participant_id: str, observer: HarnessObserver) -> bool:
        if self.otel_runtime is None:
            return False
        return self.otel_runtime.has_active(participant_id, observer.enrichment_manifests())

    def _warn_unobservable(self, pid: str, p, *, reason: str | None = None) -> None:
        if pid in self._unobservable:
            return
        self._unobservable.add(pid)
        if reason is None:
            if normalize_harness(p.harness) not in self.harnesses:
                known = ", ".join(sorted(self.harnesses)) or "none"
                reason = f"harness {p.harness!r} is not one we can read (known: {known})"
            else:
                reason = "it reported no working directory"
        logger.warning("cannot observe %s: %s", pid, reason)
