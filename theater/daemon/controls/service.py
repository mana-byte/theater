"""The harness-neutral daemon control service: the durable control state machine.

Behaviour lives in per-concern mixins; this module keeps shared state and lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Literal, TypeVar

from theater import timing
from theater.constants.daemon import CONTROL_MAINTENANCE_INTERVAL_SECONDS
from theater.constants.observability import CONTROL_DELIVERY_UNKNOWN_METRIC, MAX_ERROR_TYPE_LEN
from theater.daemon.controls._common import (
    ACTION_INTERRUPT,
    ACTION_QUEUE_DISPATCH,
    ACTION_QUEUE_FOLLOWUP,
    ACTION_SEND,
    ACTION_SETTINGS_UPDATE,
    ACTION_STEER,
    ACTION_TERMINATE,
    AMBIGUOUS_DELIVERY_DEADLINE_SECONDS,
    CONTROL_DELIVERY_REJECTED,
    CONTROL_DELIVERY_UNKNOWN,
    CONTROL_TRANSFERRED_ERROR_CODE,
    CONTROL_TRANSPORT_UNKNOWN,
    DAEMON_RESTARTED_ERROR_CODE,
    DELIVERY_UNKNOWN_ERROR_CODE,
    INTERRUPTED_ERROR_CODE,
    NATIVE_TURN_CONFLICT_ERROR_CODE,
    SEND_REJECTED_ERROR_CODE,
)
from theater.daemon.controls.activity import ActivityControls, BusyFacts
from theater.daemon.controls.admission import AdmissionControls
from theater.daemon.controls.dispatch import (
    TEMPORARY_REFUSALS,
    DispatchControls,
    QueueDispatchOutcome,
)
from theater.daemon.controls.evidence import EvidenceControls
from theater.daemon.controls.followups import FollowupControls
from theater.daemon.controls.gates import ControlGates
from theater.daemon.controls.interrupts import InterruptControls, InterruptOutcome
from theater.daemon.controls.native_delivery import NativeDeliveryControls
from theater.daemon.controls.projection import ControlActionProjector
from theater.daemon.controls.provider_delivery import ProviderControlDelivery
from theater.daemon.controls.public_admission import PublicControlAdmission
from theater.daemon.controls.recovery import RecoveryControls
from theater.daemon.controls.routing import ControlRoute, ControlRouteResolver
from theater.daemon.controls.sending import SendControls
from theater.daemon.controls.settings import SettingsControls, SettingsOutcome
from theater.daemon.controls.steering import SteerControls
from theater.daemon.jobs import JobManager
from theater.daemon.operations.notifications import OperationNotifier
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlKind,
    HarnessRuntime,
    RuntimeCapabilities,
    RuntimeCapability,
)
from theater.models import Job, now
from theater.observability.catalog import (
    CONTROL_INTERRUPT,
    CONTROL_QUEUE_FOLLOWUP,
    CONTROL_SEND,
    CONTROL_SETTINGS_UPDATE,
    CONTROL_STEER,
)
from theater.observability.engine import metric_bridge
from theater.observability.metrics import MetricKind, MetricSpec

__all__ = [
    "ACTION_INTERRUPT",
    "ACTION_QUEUE_DISPATCH",
    "ACTION_QUEUE_FOLLOWUP",
    "ACTION_SEND",
    "ACTION_SETTINGS_UPDATE",
    "ACTION_STEER",
    "ACTION_TERMINATE",
    "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS",
    "CONTROL_TRANSFERRED_ERROR_CODE",
    "DAEMON_RESTARTED_ERROR_CODE",
    "DELIVERY_UNKNOWN_ERROR_CODE",
    "INTERRUPTED_ERROR_CODE",
    "NATIVE_TURN_CONFLICT_ERROR_CODE",
    "SEND_REJECTED_ERROR_CODE",
    "TEMPORARY_REFUSALS",
    "BusyFacts",
    "ControlService",
    "InterruptOutcome",
    "QueueDispatchOutcome",
    "SettingsOutcome",
]

logger = logging.getLogger("theater.daemon.controls")

_LegacyInterruptPlan = TypeVar("_LegacyInterruptPlan")

_CONTROL_METRIC_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec(
        CONTROL_DELIVERY_UNKNOWN_METRIC,
        "Controls whose delivery stayed unknown; never retried, never fallen back.",
        "1",
        MetricKind.COUNTER,
        ("kind", "reason"),
    ),
)
_UNKNOWN_DELIVERY_SPEC = _CONTROL_METRIC_SPECS[0]

#: The latency catalog spec per public control kind.
_LATENCY_SPECS = {
    ControlKind.SEND: CONTROL_SEND,
    ControlKind.STEER: CONTROL_STEER,
    ControlKind.QUEUE_FOLLOWUP: CONTROL_QUEUE_FOLLOWUP,
    ControlKind.SETTINGS_UPDATE: CONTROL_SETTINGS_UPDATE,
    ControlKind.INTERRUPT: CONTROL_INTERRUPT,
}


def _error_type_bounded(exc_val: BaseException | None) -> str:
    """Bounded error type for the latency log — the class or error code, never
    the message, a prompt body, or any identity."""
    if exc_val is None:
        return ""
    code = getattr(exc_val, "code", None)
    text = code if isinstance(code, str) and code else type(exc_val).__name__
    return text[:MAX_ERROR_TYPE_LEN]


class _ControlLatency:
    """One public control's latency measurement — instrumentation only."""

    __slots__ = ("_participant_id", "_spec", "_start", "delivery", "transport")

    def __init__(self, spec, participant_id: str) -> None:
        self._spec = spec
        self._participant_id = participant_id
        self.delivery: str | None = None
        self.transport: str = CONTROL_TRANSPORT_UNKNOWN
        self._start: float | None = None
        with contextlib.suppress(Exception):
            self._start = time.perf_counter()

    def __enter__(self) -> _ControlLatency:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> Literal[False]:
        try:
            self._emit(exc_type, exc_val)
        except Exception:
            logger.debug("control latency emission failed", exc_info=True)
        return False

    def _emit(self, exc_type, exc_val) -> None:
        if self._spec is None or self._start is None:
            return  # no spec or no clock: nothing can be measured, fail open
        try:
            elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        except Exception:
            return
        if (
            exc_type is not None
            and isinstance(exc_type, type)
            and issubclass(exc_type, asyncio.CancelledError)
        ):
            result = "cancelled"
            delivery = self.delivery or CONTROL_DELIVERY_UNKNOWN
        elif exc_type is not None:
            result = "error"
            delivery = self.delivery or CONTROL_DELIVERY_REJECTED
        else:
            result = "success"
            delivery = self.delivery or CONTROL_DELIVERY_UNKNOWN
        timing.emit(
            self._spec,
            elapsed_ms,
            result=result,
            error_type=_error_type_bounded(exc_val),
            id=self._participant_id,
            delivery=delivery,
            transport=self.transport,
        )


class ControlService(
    AdmissionControls,
    SendControls,
    SteerControls,
    FollowupControls,
    DispatchControls,
    SettingsControls,
    InterruptControls,
    EvidenceControls,
    RecoveryControls,
    NativeDeliveryControls,
    ActivityControls,
):
    """Durable control state machine for every Theater-originated control."""

    def __init__(
        self,
        *,
        store: Store,
        jobs: JobManager,
        runtime_for: Callable[[str], HarnessRuntime | None],
        gates: ControlGates,
        route_resolver: ControlRouteResolver | None = None,
        native_route: Callable[[str, object | None], Mapping[str, object] | None] | None = None,
        native_capabilities: Callable[[str, object | None], RuntimeCapabilities | None]
        | None = None,
        native_admission: Callable[[str, object | None], Mapping[str, object] | None] | None = None,
    ) -> None:
        #: ``None`` means no live runtime.
        self._runtime_for = runtime_for
        self._store = store
        self._jobs = jobs
        self._gates = gates
        self._projection = ControlActionProjector(store, gates, self.active_job_for_native_turn)
        self._native_identity_fencing = native_route is not None
        self._routes = route_resolver or ControlRouteResolver(
            store=store,
            runtime_for=runtime_for,
            provider_health=gates.provider_health,
            native_route=native_route,
            native_capabilities=native_capabilities,
            native_admission=native_admission,
        )
        self._control_notifier = OperationNotifier()
        self._provider = ProviderControlDelivery(
            store=store,
            jobs=jobs,
            gates=gates,
            route_for=self.route_for,
            terminal_route_for=self.terminal_route_for,
            reserve=self._reserve,
            clock=self._clock,
            count_unknown=self._count_unknown_delivery,
            notify_settled=self._control_notifier.notify,
        )
        self._public_admission = PublicControlAdmission(store, clock=self._clock)
        self._locks: dict[str, asyncio.Lock] = {}
        self._dispatch_tasks: dict[str, asyncio.Task[QueueDispatchOutcome]] = {}
        # The one-shot dispatch task above preserves the immediate queue opportunity for callers.
        self._maintenance_tasks: dict[str, asyncio.Task[None]] = {}
        self._maintenance_wakeups: dict[str, asyncio.Event] = {}
        self._maintenance_versions: dict[str, int] = {}
        # A restart must let the reconnected live observer route buffered, exact terminal evidence
        # before an old wall-clock deadline can close a job.
        self._deadline_not_before: dict[str, float] = {}
        self._scheduler_started = False
        self._recovering = False
        self._closing = False
        self._register_metric_specs()

    # ---- lifecycle-owned maintenance ------------------------------------

    def begin_recovery(self) -> None:
        """Tell reconciliation that startup evidence must win over deadlines."""
        self._recovering = True

    def start(self, participant_ids: Iterable[str] = ()) -> None:
        """Start coalesced per-participant maintenance after observer startup."""
        if self._closing:
            return
        recovery_floor = self._recovering
        self._recovering = False
        self._scheduler_started = True
        floor = self._clock() + AMBIGUOUS_DELIVERY_DEADLINE_SECONDS
        for participant_id in participant_ids:
            barriers = self._store.execution_barrier_control_operations(participant_id)
            pending = self._store.unresolved_prompt_delivery_operations(participant_id)
            if recovery_floor:
                operations = {
                    operation.operation_id: operation for operation in (*barriers, *pending)
                }
                for operation in operations.values():
                    self._deadline_not_before[operation.operation_id] = floor
            if barriers or pending or self._store.queued_control_operation_count(participant_id):
                self._schedule_maintenance(participant_id)

    async def aclose(self) -> None:
        """Cancel and await every service-owned scheduler/wakeup task."""
        self._closing = True
        self._scheduler_started = False
        tasks = {
            *self._dispatch_tasks.values(),
            *self._maintenance_tasks.values(),
        }
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatch_tasks.clear()
        self._maintenance_tasks.clear()
        self._maintenance_wakeups.clear()
        self._maintenance_versions.clear()
        self._deadline_not_before.clear()

    @property
    def owned_tasks(self) -> tuple[asyncio.Task, ...]:
        """Current scheduler tasks, exposed only for deterministic teardown tests."""
        return tuple(
            task
            for task in (*self._dispatch_tasks.values(), *self._maintenance_tasks.values())
            if not task.done()
        )

    # ---- observability (instrumentation only) ---------------------------

    def _register_metric_specs(self) -> None:
        """Register the control counter on the process's one metric bridge."""
        with contextlib.suppress(Exception):
            bridge = metric_bridge()
            if bridge is not None:
                bridge.register_specs(_CONTROL_METRIC_SPECS)

    def _count_unknown_delivery(self, kind: ControlKind, reason: str) -> None:
        """Count one unknown delivery as an explicit bounded outcome/reason."""
        with contextlib.suppress(Exception):
            bridge = metric_bridge()
            if bridge is not None:
                bridge.observe(_UNKNOWN_DELIVERY_SPEC, 1, {"kind": kind.value, "reason": reason})

    def _control_latency(self, kind: ControlKind, participant_id: str) -> _ControlLatency:
        """Time one public control; setup is fail-open and can never raise."""
        spec = None
        with contextlib.suppress(Exception):
            spec = _LATENCY_SPECS[kind]
        return _ControlLatency(spec, participant_id)

    def route_for(
        self, participant_id: str, capability: RuntimeCapability, *, connection=None
    ) -> ControlRoute:
        """Return the durable route selected for one capability."""
        return self._routes.resolve(participant_id, capability, connection=connection)

    def terminal_route_for(self, participant_id: str, *, connection=None) -> ControlRoute:
        return self._routes.terminal_route(participant_id, connection=connection)

    def _schedule_maintenance(self, participant_id: str) -> None:
        """Wake one bounded background maintainer for ``participant_id``."""
        if not self._scheduler_started or self._closing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._maintenance_versions[participant_id] = (
            self._maintenance_versions.get(participant_id, 0) + 1
        )
        wake = self._maintenance_wakeups.setdefault(participant_id, asyncio.Event())
        wake.set()
        task = self._maintenance_tasks.get(participant_id)
        if task is None or task.done():
            self._maintenance_tasks[participant_id] = loop.create_task(
                self._maintenance_loop(participant_id)
            )

    async def _maintenance_loop(self, participant_id: str) -> None:
        """Reconcile one participant's barriers and deferred FIFO head."""
        task = asyncio.current_task()
        try:
            while not self._closing:
                version = self._maintenance_versions.get(participant_id, 0)
                try:
                    await self._maintenance_once(participant_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("control maintenance pass for %s failed", participant_id)
                if self._closing:
                    return
                has_work = self._has_maintenance_work(participant_id)
                current_version = self._maintenance_versions.get(participant_id, 0)
                if not has_work and current_version == version:
                    return
                # A request that arrived while the pass awaited runtime I/O should be processed
                # immediately.
                if current_version != version:
                    continue
                wake = self._maintenance_wakeups[participant_id]
                wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        wake.wait(), timeout=CONTROL_MAINTENANCE_INTERVAL_SECONDS
                    )
        finally:
            # No await before removal: concurrent schedules either bump the version or create a
            # successor.
            if self._maintenance_tasks.get(participant_id) is task:
                self._maintenance_tasks.pop(participant_id, None)
                self._maintenance_wakeups.pop(participant_id, None)
                self._maintenance_versions.pop(participant_id, None)

    async def _maintenance_once(self, participant_id: str) -> None:
        """One read-only recovery pass followed by at most one FIFO dispatch."""
        if self._store.has_execution_barrier(
            participant_id
        ) or self._store.unresolved_prompt_delivery_operations(participant_id):
            await self.reconcile_ambiguous_delivery(participant_id, now_ts=self._clock())
        # A still-unresolved execution is a hard boundary: never let the
        # queued head reach native or legacy delivery while it remains.
        if self._store.has_execution_barrier(participant_id):
            return
        if self._store.queued_control_operation_count(participant_id):
            await self.dispatch_queue(participant_id)

    def _has_maintenance_work(self, participant_id: str) -> bool:
        """The durable predicate that bounds one participant's task lifetime."""
        return bool(
            self._store.has_execution_barrier(participant_id)
            or self._store.unresolved_prompt_delivery_operations(participant_id)
            or self._store.queued_control_operation_count(participant_id)
        )

    def _lock(self, participant_id: str) -> asyncio.Lock:
        """One lock per participant, and nothing global, ever."""
        return self._locks.setdefault(participant_id, asyncio.Lock())

    def _clock(self) -> float:
        return now()

    def _mint_sequence(self, *, connection=None) -> int:
        """One position from the persisted send-sequence allocator."""
        return self._store.allocate_control_queue_sequence(connection=connection)

    def _mint_operation_id(self, participant_id: str, kind: ControlKind) -> str:
        sequence = self._mint_sequence()
        return f"{participant_id}#{sequence}:{kind.value}"

    def _require_job(self, handle: str) -> Job:
        job = self._store.get_job(handle)
        assert job is not None
        return job
