"""The harness-neutral daemon control service: the durable control state machine."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from typing import Literal, TypeVar

from theater import timing
from theater.constants.daemon import (
    CONTROL_AMBIGUOUS_DELIVERY_DEADLINE_SECONDS,
    CONTROL_MAINTENANCE_INTERVAL_SECONDS,
    CONTROL_QUEUE_MAX_PENDING,
)
from theater.constants.observability import (
    CONTROL_DELIVERY_UNKNOWN_METRIC,
    MAX_ERROR_TYPE_LEN,
)
from theater.daemon.controls.busy import (
    BusyAction,
    BusyOperation,
    BusyRefusal,
    busy_refusal,
)
from theater.daemon.controls.gates import ControlGates
from theater.daemon.controls.projection import ControlActionProjector
from theater.daemon.controls.provider_delivery import ProviderControlDelivery
from theater.daemon.controls.provider_interrupt import interrupt_action
from theater.daemon.controls.public_admission import (
    PublicControlAdmission,
    PublicControlReservation,
)
from theater.daemon.controls.routing import ControlRoute, ControlRouteResolver
from theater.daemon.events.publication import control_event, next_revision
from theater.daemon.jobs import JobManager
from theater.daemon.operations.notifications import OperationNotifier
from theater.daemon.persistence.repositories.control_operations import (
    ControlOperation,
    ControlOperationAmbiguityError,
)
from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    ControlDeliveryPhase,
    ControlKind,
    ControlReceipt,
    ControlTransport,
    DeliveryResult,
    HarnessRuntime,
    NativeTurnOutcome,
    NativeTurnTerminal,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeExecutionState,
    RuntimeSettingField,
    RuntimeSnapshot,
)
from theater.models import (
    AwaitingDecision,
    BadRequest,
    Busy,
    HumanPresent,
    Job,
    JobState,
    NotAddressable,
    StaleTarget,
    Status,
    now,
)
from theater.observability.catalog import (
    CONTROL_INTERRUPT,
    CONTROL_QUEUE_FOLLOWUP,
    CONTROL_SEND,
    CONTROL_SETTINGS_UPDATE,
    CONTROL_STEER,
    LIFECYCLE_STAGE,
)
from theater.observability.engine import metric_bridge
from theater.observability.metrics import MetricKind, MetricSpec

__all__ = [
    "AMBIGUOUS_DELIVERY_DEADLINE_SECONDS",
    "ControlService",
    "InterruptOutcome",
    "QueueDispatchOutcome",
    "SettingsOutcome",
]

logger = logging.getLogger("theater.daemon.controls")

_LegacyInterruptPlan = TypeVar("_LegacyInterruptPlan")

#: Compatibility export for existing callers.
AMBIGUOUS_DELIVERY_DEADLINE_SECONDS = CONTROL_AMBIGUOUS_DELIVERY_DEADLINE_SECONDS

DAEMON_RESTARTED_ERROR_CODE = "daemon_restarted"
DELIVERY_UNKNOWN_ERROR_CODE = "delivery_unknown"
INTERRUPTED_ERROR_CODE = "interrupted"
CONTROL_TRANSFERRED_ERROR_CODE = "control_transferred"
NATIVE_TURN_CONFLICT_ERROR_CODE = "native_turn_conflict"
SEND_REJECTED_ERROR_CODE = "send_rejected"

#: Delivery outcome labels for the control latency metric.
CONTROL_DELIVERY_ACCEPTED = DeliveryResult.ACCEPTED.value
CONTROL_DELIVERY_REJECTED = DeliveryResult.REJECTED.value
CONTROL_DELIVERY_UNKNOWN = DeliveryResult.UNKNOWN.value
CONTROL_DELIVERY_QUEUED = "queued"

#: Bounded transport label while the original body has not established a transport — every refusal
#: raised before classification, a disconnected native, a legacy path that never reached delivery.
CONTROL_TRANSPORT_UNKNOWN = "unknown"

#: Bounded reasons an unknown delivery is counted.
CONTROL_UNKNOWN_ACK_LOST = "ack_lost"
CONTROL_UNKNOWN_RECEIPT_MISMATCH = "receipt_mismatch"
CONTROL_UNKNOWN_RECEIPT_UNKNOWN = "receipt_unknown"
CONTROL_UNKNOWN_UNCORRELATED = "uncorrelated"
CONTROL_UNKNOWN_READBACK_FAILED = "readback_failed"
CONTROL_UNKNOWN_RESTART = "restart"
CONTROL_UNKNOWN_DEADLINE = "deadline"

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

#: The job result for a prompt that never began transmission before the
#: daemon restart: failed, never replayed, and safe to send again.
_UNDELIVERED_RESTART_RESULT = (
    "Send failed: the Theater daemon restarted before the prompt was "
    "transmitted, and an undelivered prompt is never replayed. Send it "
    "again if the work is still wanted."
)

#: Refusals that are temporary: a queued followup hit by one stays queued.
TEMPORARY_REFUSALS = (Busy, HumanPresent, AwaitingDecision)

_JOB_STATE_FOR_TERMINAL = {
    NativeTurnTerminal.COMPLETED: JobState.DONE,
    NativeTurnTerminal.FAILED: JobState.CRASHED,
    NativeTurnTerminal.INTERRUPTED: JobState.KILLED,
}

#: The actions the authorize gate is asked about.
ACTION_SEND = "send"
ACTION_STEER = "steer"
ACTION_QUEUE_FOLLOWUP = "queue_followup"
ACTION_QUEUE_DISPATCH = "queue_dispatch"
ACTION_SETTINGS_UPDATE = "settings_update"
ACTION_INTERRUPT = "interrupt"
ACTION_TERMINATE = "terminate"


@dataclass(frozen=True, slots=True)
class SettingsOutcome:
    """What one settings update established."""

    applied: bool | None
    model: str | None = None
    reasoning_effort: str | None = None
    error_code: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class InterruptOutcome:
    """What one interrupt did."""

    #: Whether an interruption was requested and accepted.
    interrupted: bool
    #: ``already_idle`` when there was no active turn to interrupt.
    reason: str | None = None
    #: Job handles cancelled out of the queue before they were ever delivered.
    cancelled_followups: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class QueueDispatchOutcome:
    """One dispatch pass over a participant's followup queue."""

    #: Job handles dispatched this pass (at most one — prompts go one at a
    #: time; later passes are triggered by terminal evidence).
    dispatched: tuple[str, ...] = ()
    #: ``(job_handle, error_code)`` for items that failed definitively.
    failed: tuple[tuple[str, str], ...] = ()
    #: True when the queue head stayed queued on a temporary condition.
    deferred: bool = False


@dataclass(frozen=True, slots=True)
class BusyFacts:
    """The snapshot and store facts one busy-refusal walk consults."""

    idle: bool
    connected: bool
    identified: bool
    active: bool
    queued: int
    barrier: bool
    running_handle: str | None = None


def _error_type_bounded(exc_val: BaseException | None) -> str:
    """Bounded error type for the latency log — the class or error code, never
    the message, a prompt body, or any identity."""
    if exc_val is None:
        return ""
    code = getattr(exc_val, "code", None)
    text = code if isinstance(code, str) and code else type(exc_val).__name__
    return text[:MAX_ERROR_TYPE_LEN]


def _delivery_label(result: DeliveryResult | None) -> str:
    """Map a settled receipt result to the latency outcome label."""
    return result.value if result is not None else CONTROL_DELIVERY_UNKNOWN


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


class ControlService:
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

    async def _require_absent(self, participant_id: str, route: ControlRoute) -> None:
        """Admit a provider route on cached absence: the provider re-verifies presence
        (``require_absent``) immediately before its terminal effect. Other routes,
        and any cached non-absence, need fresh evidence."""
        if route.is_provider:
            with contextlib.suppress(HumanPresent):
                self._gates.check_absent(participant_id)
                return
        await self._gates.require_absent(participant_id)

    def reserve_public_control(
        self,
        unit,
        *,
        operation_id: str,
        participant_id: str,
        kind: ControlKind,
        route: ControlRoute,
        caller_id: str,
        actor_client_id: str,
        prompt: str | None = None,
        response_format: str | None = None,
        expected_turn_id: str | None = None,
        settings: dict[str, str] | None = None,
    ) -> PublicControlReservation:
        """Reserve the complete public admission group without external I/O."""
        self._gates.authorize(participant_id, caller_id, kind.value)
        if prompt is not None:
            self._gates.check_prompt(prompt)
        if kind is ControlKind.SETTINGS_UPDATE:
            self._gates.check_settings(
                None if settings is None else settings.get("model"),
                None if settings is None else settings.get("reasoning_effort"),
            )
        if route.is_provider:
            self._provider.require(participant_id, route.capability, route)
        return self._public_admission.reserve(
            unit,
            operation_id=operation_id,
            participant_id=participant_id,
            kind=kind,
            route=route,
            caller_id=caller_id,
            actor_client_id=actor_client_id,
            prompt=prompt,
            response_format=response_format,
            expected_turn_id=expected_turn_id,
            settings=settings,
        )

    async def wait_control_settled(self, operation_id: str) -> ControlOperation | None:
        """Wait without polling; callers always re-read the durable row."""
        current = self._store.get_control_operation(operation_id)
        if current is None or current.delivery_phase is ControlDeliveryPhase.SETTLED:
            return current
        subscription = self._control_notifier.subscribe(operation_id)
        try:
            current = self._store.get_control_operation(operation_id)
            if current is not None and current.delivery_phase is not ControlDeliveryPhase.SETTLED:
                await subscription.wait()
            return self._store.get_control_operation(operation_id)
        finally:
            subscription.close()

    def notify_persisted_settlement(self, operation_id: str) -> None:
        """Wake control waiters after a caller-owned write unit commits."""
        self._control_notifier.notify(operation_id)

    async def terminate_provider(
        self,
        participant_id: str,
        *,
        caller_id: str,
        callback_operation_id: str,
    ) -> Mapping[str, object]:
        waiting_since = time.perf_counter()
        async with self._lock(participant_id):
            timing.emit(
                LIFECYCLE_STAGE,
                (time.perf_counter() - waiting_since) * 1000,
                action="kill",
                stage="lock_wait",
                id=participant_id,
                operation_id=callback_operation_id,
            )
            self._gates.authorize(participant_id, caller_id, ACTION_TERMINATE)
            route = self.terminal_route_for(participant_id)
            with timing.span(
                LIFECYCLE_STAGE,
                action="kill",
                stage="presence",
                id=participant_id,
                operation_id=callback_operation_id,
            ):
                await self._require_absent(participant_id, route)
            terminal = self._provider.require(
                participant_id, RuntimeCapability.INTERRUPT, route, terminal_only=True
            )
            dispatch = self._gates.provider_dispatch
            if dispatch is None:
                raise StaleTarget("provider callback transport is not composed")
            self._gates.check_absent(participant_id)
            with timing.span(
                LIFECYCLE_STAGE,
                action="kill",
                stage="provider",
                id=participant_id,
                operation_id=callback_operation_id,
            ):
                return await dispatch(
                    terminal.provider_id,
                    terminal.provider_generation,
                    "terminal.terminate",
                    {
                        "operation_id": callback_operation_id,
                        "provider_generation": terminal.provider_generation,
                        "participant_id": participant_id,
                        "terminal_id": terminal.terminal_id,
                        "terminal_incarnation": terminal.terminal_incarnation,
                        "expected_occupant": terminal.occupant_evidence["occupant_id"],
                        "require_absent": True,
                    },
                )

    # ---- ordinary send ---------------------------------------------------

    async def send(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None = None,
        job_handle: str | None = None,
        operation_id: str | None = None,
        callback_operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
        pre_reserved: bool = False,
    ) -> Job:
        """Ordinary send — or the native initial dispatch of one spawn job."""
        with self._control_latency(ControlKind.SEND, participant_id) as latency:
            job, delivery, transport = await self._send(
                participant_id,
                caller_id=caller_id,
                prompt=prompt,
                response_format=response_format,
                job_handle=job_handle,
                operation_id=operation_id,
                callback_operation_id=callback_operation_id,
                on_reserved=on_reserved,
                actor_client_id=actor_client_id,
                actor_participant_id=actor_participant_id,
                pre_reserved=pre_reserved,
            )
            latency.delivery = delivery
            latency.transport = transport
            return job

    async def _send(  # noqa: PLR0912, PLR0915
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
        job_handle: str | None,
        operation_id: str | None,
        callback_operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        actor_client_id: str | None,
        actor_participant_id: str | None,
        pre_reserved: bool,
    ) -> tuple[Job, str, str]:
        """The send body; returns its job, delivery label, and transport."""
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_SEND)
            route = self.route_for(participant_id, RuntimeCapability.SEND)
            if route.transport is None:
                raise NotAddressable(
                    f"participant {participant_id!r} does not offer a transport for sending"
                )
            # A spawn's native initial dispatch targets a brand-new participant
            # the same request just created; presence never gates it.
            initial_dispatch = job_handle is not None
            if not initial_dispatch:
                await self._require_absent(participant_id, route)
            self._gates.check_prompt(prompt)
            await self._gates.send_preflight(participant_id)
            if route.is_provider:
                reserved = (
                    self._require_public_reservation(
                        operation_id,
                        participant_id=participant_id,
                        kind=ControlKind.SEND,
                        phase=ControlDeliveryPhase.RESERVED,
                        route=route,
                    )
                    if pre_reserved
                    else None
                )
                if reserved is not None:
                    assert reserved.job_handle is not None
                    provider_job = self._require_job(reserved.job_handle)
                elif job_handle is not None:
                    provider_job = self._reusable_spawn_job(
                        participant_id,
                        job_handle=job_handle,
                        caller_id=caller_id,
                        prompt=prompt,
                        response_format=response_format,
                    )
                else:
                    await self._gates.legacy_busy_check(participant_id)
                    self._reject_provider_send_busy(participant_id, exclude=None)
                    provider_job = self._create_send_job(
                        participant_id,
                        caller_id=caller_id,
                        prompt=prompt,
                        response_format=response_format,
                        actor_client_id=actor_client_id,
                        actor_participant_id=actor_participant_id,
                    )
                if not initial_dispatch:
                    self._reject_provider_send_busy(
                        participant_id,
                        exclude=provider_job.handle,
                    )
                control_id = operation_id or self._mint_operation_id(
                    participant_id, ControlKind.SEND
                )
                if reserved is None:
                    self._provider.reserve(
                        control_id,
                        route,
                        participant_id=participant_id,
                        kind=ControlKind.SEND,
                        phase=ControlDeliveryPhase.RESERVED,
                        job_handle=provider_job.handle,
                    )
                    self._notify_reserved(on_reserved, control_id, provider_job.handle)
                provider_delivery = await self._provider.deliver(
                    route,
                    capability=RuntimeCapability.SEND,
                    kind=ControlKind.SEND,
                    participant_id=participant_id,
                    control_operation_id=control_id,
                    callback_operation_id=callback_operation_id or control_id,
                    action={"kind": "submit_text", "text": prompt},
                    job_handle=provider_job.handle,
                )
                return (
                    self._require_job(provider_job.handle),
                    _delivery_label(provider_delivery),
                    ControlTransport.PROVIDER_TERMINAL.value,
                )
            if route.is_legacy:
                if job_handle is not None:
                    raise BadRequest(
                        f"reusing job {job_handle!r} for the initial dispatch of "
                        f"participant {participant_id!r} requires native runtime "
                        "wiring; its harness has no runtime, so the prompt can only "
                        "be sent as an ordinary send"
                    )
                legacy_job = await self._send_legacy(
                    participant_id,
                    caller_id=caller_id,
                    prompt=prompt,
                    response_format=response_format,
                    operation_id=operation_id,
                    on_reserved=on_reserved,
                    actor_client_id=actor_client_id,
                    actor_participant_id=actor_participant_id,
                )
                return legacy_job, CONTROL_DELIVERY_ACCEPTED, ControlTransport.LEGACY_TMUX.value
            if not route.is_native:
                raise NotAddressable(
                    f"participant {participant_id!r} does not offer a transport for sending"
                )
            if runtime is None:
                raise self._disconnected_native_refusal(participant_id, "send")
            # The reused spawn job is validated before any runtime I/O: a
            # wrong handle fails closed with nothing sent and nothing minted.
            reserved = (
                self._require_public_reservation(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.SEND,
                    phase=ControlDeliveryPhase.RESERVED,
                    route=route,
                )
                if pre_reserved
                else None
            )

            job: Job | None = None
            if reserved is not None:
                assert reserved.job_handle is not None
                job = self._require_job(reserved.job_handle)
            elif job_handle is not None:
                job = self._reusable_spawn_job(
                    participant_id,
                    job_handle=job_handle,
                    caller_id=caller_id,
                    prompt=prompt,
                    response_format=response_format,
                )
            snapshot = await self._snapshot_for_control(
                runtime, participant_id, initial_dispatch=initial_dispatch
            )
            route = self._require_current_native_route(
                participant_id, RuntimeCapability.SEND, snapshot
            )
            self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
            if reserved is not None:
                self._require_reserved_native_identity(reserved, snapshot)
            self._reject_busy(
                participant_id,
                snapshot,
                operation=BusyOperation.SEND,
                exclude=job.handle if job is not None else None,
            )
            if job is None:
                job = self._create_send_job(
                    participant_id,
                    caller_id=caller_id,
                    prompt=prompt,
                    response_format=response_format,
                    actor_client_id=actor_client_id,
                    actor_participant_id=actor_participant_id,
                )
            operation_id = operation_id or self._mint_operation_id(participant_id, ControlKind.SEND)
            if reserved is None:
                self._reserve(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.SEND,
                    transport=ControlTransport.NATIVE_RUNTIME,
                    phase=ControlDeliveryPhase.RESERVED,
                    job_handle=job.handle,
                    backend_generation=snapshot.backend_generation,
                    native_session_id=snapshot.native_session_id,
                )
                self._notify_reserved(on_reserved, operation_id, job.handle)
            cwd = self._gates.cwd_for(participant_id)
            if pre_reserved and cwd is not None:
                self._jobs.attach_touch_accumulator(job.handle, cwd=cwd)
            delivery = await self._deliver_native(
                runtime,
                kind=ControlKind.SEND,
                participant_id=participant_id,
                operation_id=operation_id,
                prompt=prompt,
                job_handle=job.handle,
                snapshot=snapshot,
            )
            return (
                self._require_job(job.handle),
                _delivery_label(delivery),
                ControlTransport.NATIVE_RUNTIME.value,
            )

    def _reject_provider_send_busy(self, participant_id: str, *, exclude: str | None) -> None:
        """Serialize provider sends behind accepted work and uncertain execution."""
        if self._store.has_execution_barrier(participant_id):
            raise Busy(
                f"participant {participant_id!r} has an unresolved delivery; "
                "reconcile it before sending another prompt"
            )
        queued = self._store.queued_control_operation_count(participant_id)
        if queued:
            raise Busy(
                f"participant {participant_id!r} has {queued} queued followup(s); "
                "an ordinary send cannot jump ahead of them"
            )
        participant = self._store.get_participant(participant_id)
        if participant is not None and participant.status is Status.WORKING:
            raise Busy(f"participant {participant_id!r} is working; not delivering now")
        running = self._store.active_running_jobs_for_target(participant_id)
        if any(job.handle != exclude and job.prompt for job in running):
            raise Busy(f"participant {participant_id!r} has a running send job")

    async def _snapshot_for_control(
        self, runtime: HarnessRuntime, participant_id: str, *, initial_dispatch: bool = False
    ) -> RuntimeSnapshot:
        """Refresh focus first, then read runtime state and recheck focus without yielding."""
        if not initial_dispatch:
            await self._gates.require_absent(participant_id)
        snapshot = await runtime.snapshot()
        if not initial_dispatch:
            self._gates.check_absent(participant_id)
        if runtime is not self._runtime_for(participant_id):
            raise StaleTarget(f"runtime for {participant_id!r} changed during control preparation")
        self._gates.record_native_snapshot(participant_id, runtime, snapshot)
        return snapshot

    def _require_current_native_route(
        self,
        participant_id: str,
        capability: RuntimeCapability,
        snapshot: RuntimeSnapshot,
        *,
        require_available: bool = True,
    ) -> ControlRoute:
        """Fence production native controls to the exact durable cached session."""
        route = self.route_for(participant_id, capability)
        if not route.is_native:
            raise StaleTarget(
                f"the native route for participant {participant_id!r} changed during preparation"
            )
        if not self._native_identity_fencing:
            return route
        native = route.native_route
        if native is None or (require_available and not route.route_available):
            raise StaleTarget(
                f"the exact native runtime route for participant {participant_id!r} is unavailable"
            )
        if (
            snapshot.native_session_id is None
            or native.get("backend_generation") != snapshot.backend_generation
            or native.get("native_session_id") != snapshot.native_session_id
        ):
            raise StaleTarget(
                f"the native runtime identity for participant {participant_id!r} changed "
                "during control preparation"
            )
        return route

    async def _send_legacy(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
        operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
    ) -> Job:
        """Legacy transport: the same durable receipt transitions, no runtime."""
        await self._gates.require_absent(participant_id)
        await self._gates.legacy_copy_mode_check(participant_id)
        # Recheck after the awaited copy-mode query, before any durable effect.
        await self._gates.require_absent(participant_id)
        # Keep FIFO refusal ordering: a queued followup refuses the send before any job row or
        # typing, so it stays behind the queue; queue dispatch skips _send_legacy, so no self-block.
        queued = self._store.queued_control_operation_count(participant_id)
        if queued:
            raise Busy(
                f"participant {participant_id!r} has {queued} queued followup(s); "
                "an ordinary send cannot jump ahead of them — await the queued "
                "handles or queue another followup instead"
            )
        # The busy/claim check mutates claim rows; it runs after all awaited
        # prep, its synchronous body adjacent to reservation and delivery.
        participant = self._store.get_participant(participant_id)
        if participant is not None and participant.status is Status.WORKING:
            message = f"participant {participant_id!r} is working; not injecting a new prompt."
            if participant.parent_id == caller_id:
                message += (
                    f" Call interrupt_session(target={participant_id!r}), wait until "
                    "list_participants reports status='idle', then retry send."
                )
            else:
                message += (
                    " Wait until list_participants reports status='idle', then retry send; "
                    "only the participant's direct parent may interrupt it."
                )
            raise Busy(message)
        await self._gates.legacy_busy_check(participant_id)
        job = self._create_send_job(
            participant_id,
            caller_id=caller_id,
            prompt=prompt,
            response_format=response_format,
            actor_client_id=actor_client_id,
            actor_participant_id=actor_participant_id,
        )
        operation_id = operation_id or self._mint_operation_id(participant_id, ControlKind.SEND)
        self._reserve(
            operation_id,
            participant_id=participant_id,
            kind=ControlKind.SEND,
            transport=ControlTransport.LEGACY_TMUX,
            phase=ControlDeliveryPhase.RESERVED,
            job_handle=job.handle,
        )
        self._notify_reserved(on_reserved, operation_id, job.handle)
        self._store.mark_control_operation_dispatched(operation_id, updated_at=self._clock())
        try:
            await self._gates.legacy_deliver(participant_id, prompt)
        except Exception as exc:
            # Nothing was delivered, so nothing will ever answer.
            self._store.settle_control_operation(
                operation_id,
                result=DeliveryResult.REJECTED,
                error_code="send_failed",
                error=str(exc),
                updated_at=self._clock(),
            )
            self._jobs.finish(
                job.handle,
                state=JobState.CRASHED,
                result=str(exc),
                error_code="send_failed",
            )
            raise
        self._store.settle_control_operation(
            operation_id, result=DeliveryResult.ACCEPTED, updated_at=self._clock()
        )
        self._record_legacy_send(participant_id, caller_id=caller_id, job=job, prompt=prompt)
        return self._require_job(job.handle)

    # ---- steering --------------------------------------------------------

    async def steer(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        job_handle: str | None = None,
        expected_turn_id: str | None = None,
        operation_id: str | None = None,
        callback_operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        pre_reserved: bool = False,
    ) -> Job:
        """Amend exactly the current Theater job's active native turn."""
        with self._control_latency(ControlKind.STEER, participant_id) as latency:
            job, delivery = await self._steer(
                participant_id,
                caller_id=caller_id,
                prompt=prompt,
                job_handle=job_handle,
                expected_turn_id=expected_turn_id,
                operation_id=operation_id,
                callback_operation_id=callback_operation_id,
                on_reserved=on_reserved,
                pre_reserved=pre_reserved,
            )
            latency.delivery = delivery
            route = self.route_for(participant_id, RuntimeCapability.STEER)
            latency.transport = (
                route.transport.value if route.transport else CONTROL_TRANSPORT_UNKNOWN
            )
            return job

    async def _steer(  # noqa: PLR0912, PLR0915
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        job_handle: str | None,
        expected_turn_id: str | None,
        operation_id: str | None,
        callback_operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        pre_reserved: bool,
    ) -> tuple[Job, str]:
        """The steer body; returns its job and the delivery outcome label."""
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_STEER)
            route = self.route_for(participant_id, RuntimeCapability.STEER)
            await self._require_absent(participant_id, route)
            self._gates.check_prompt(prompt)
            if route.is_provider:
                return await self._steer_provider(
                    participant_id,
                    route,
                    prompt=prompt,
                    job_handle=job_handle,
                    expected_turn_id=expected_turn_id,
                    operation_id=operation_id,
                    callback_operation_id=callback_operation_id,
                    on_reserved=on_reserved,
                    pre_reserved=pre_reserved,
                )
            if not route.is_native:
                raise self._steer_route_refusal(participant_id, route)
            if runtime is None:
                raise self._disconnected_native_refusal(participant_id, "steer")
            snapshot = await self._snapshot_for_control(runtime, participant_id)
            route = self._require_current_native_route(
                participant_id, RuntimeCapability.STEER, snapshot
            )
            self._require_capability(participant_id, snapshot, RuntimeCapability.STEER, "steering")
            expected_turn = self._require_expected_turn(
                participant_id, snapshot.native_turn_id, expected_turn_id
            )
            operation = self._operation_for_snapshot_turn(participant_id, snapshot)
            if operation is None or operation.job_handle is None:
                raise StaleTarget(
                    f"the active native turn of participant {participant_id!r} belongs "
                    "to no Theater job (a human started it in the native UI); steering "
                    "refuses instead of creating a synthetic job"
                )
            if job_handle is not None and operation.job_handle != job_handle:
                raise StaleTarget(
                    f"the active native turn of participant {participant_id!r} maps to "
                    f"job {operation.job_handle!r}, not {job_handle!r}; refusing to "
                    "amend a different job than the one you expect"
                )
            job = self._require_job(operation.job_handle)
            if job.state != JobState.RUNNING:
                raise StaleTarget(
                    f"the active native turn of participant {participant_id!r} maps to "
                    f"job {job.handle!r}, which is already {job.state}; nothing to amend"
                )
            operation_id = operation_id or self._mint_operation_id(
                participant_id, ControlKind.STEER
            )
            if pre_reserved:
                reserved = self._require_public_reservation(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.STEER,
                    phase=ControlDeliveryPhase.RESERVED,
                    route=route,
                    job_handle=job.handle,
                )
                self._require_reserved_native_identity(reserved, snapshot)
            else:
                self._reserve(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.STEER,
                    transport=ControlTransport.NATIVE_RUNTIME,
                    phase=ControlDeliveryPhase.RESERVED,
                    job_handle=job.handle,
                    backend_generation=snapshot.backend_generation,
                    native_session_id=snapshot.native_session_id,
                    native_turn_id=expected_turn,
                    payload=prompt,
                )
                self._notify_reserved(on_reserved, operation_id, job.handle)
            self._store.mark_control_operation_dispatched(
                operation_id,
                native_session_id=snapshot.native_session_id,
                native_turn_id=expected_turn,
                updated_at=self._clock(),
            )
            try:
                receipt = await runtime.steer(
                    operation_id=operation_id, native_turn_id=expected_turn, prompt=prompt
                )
            except asyncio.CancelledError:
                # The amendment may have crossed the native write before its caller was cancelled.
                self._settle_uncertain(
                    operation_id,
                    error=(
                        "the steering control was cancelled after transmission began; "
                        "its acknowledgement is unknown and it is never retried"
                    ),
                )
                self._count_unknown_delivery(ControlKind.STEER, CONTROL_UNKNOWN_ACK_LOST)
                raise
            except Exception as exc:
                # UNKNOWN steer never finishes the original prompt job; only exact terminal evidence
                # does.
                self._store.settle_control_operation(
                    operation_id,
                    result=DeliveryResult.UNKNOWN,
                    error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                    error=str(exc),
                    updated_at=self._clock(),
                )
                logger.warning(
                    "steer delivery for %s job %s is uncertain: %s",
                    participant_id,
                    job.handle,
                    exc,
                )
                self._count_unknown_delivery(ControlKind.STEER, CONTROL_UNKNOWN_ACK_LOST)
                return job, CONTROL_DELIVERY_UNKNOWN
            if not self._receipt_names_operation(operation_id, receipt):
                # The amendment cannot be confirmed for this operation; the steer stays uncertain
                # and is never retried.
                self._settle_uncertain(
                    operation_id,
                    error=(
                        f"the steering receipt named operation {receipt.operation_id!r}, "
                        f"not {operation_id!r}; the amendment is uncertain and the "
                        "receipt is not trusted to settle it"
                    ),
                )
                self._count_unknown_delivery(ControlKind.STEER, CONTROL_UNKNOWN_RECEIPT_MISMATCH)
                return job, CONTROL_DELIVERY_UNKNOWN
            self._settle_from_receipt(operation_id, receipt)
            if receipt.result is DeliveryResult.REJECTED:
                detail = receipt.error or "the expected turn is no longer active"
                raise StaleTarget(
                    f"participant {participant_id!r} refused the steering amendment "
                    f"({receipt.error_code or 'rejected'}: {detail}); the refusal is "
                    "final — queue a followup instead of retrying the steer"
                )
            if receipt.result is DeliveryResult.UNKNOWN:
                logger.warning(
                    "steer delivery for %s job %s stayed uncertain",
                    participant_id,
                    job.handle,
                )
                self._count_unknown_delivery(ControlKind.STEER, CONTROL_UNKNOWN_RECEIPT_UNKNOWN)
            return job, _delivery_label(receipt.result)

    async def _steer_provider(
        self,
        participant_id: str,
        route: ControlRoute,
        *,
        prompt: str,
        job_handle: str | None,
        expected_turn_id: str | None,
        operation_id: str | None,
        callback_operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        pre_reserved: bool,
    ) -> tuple[Job, str]:
        jobs = self._store.active_running_jobs_for_target(participant_id)
        if len(jobs) != 1:
            raise StaleTarget(
                f"participant {participant_id!r} does not have exactly one active Theater job "
                "to steer"
            )
        job = jobs[0]
        if job_handle is not None and job.handle != job_handle:
            raise StaleTarget(
                f"participant {participant_id!r} is running job {job.handle!r}, not {job_handle!r}"
            )
        terminal = self._provider.require(participant_id, RuntimeCapability.STEER, route)
        observed_turn = terminal.occupant_evidence.get("turn_id")
        if expected_turn_id is not None and observed_turn != expected_turn_id:
            raise StaleTarget(
                f"participant {participant_id!r} no longer reports expected turn "
                f"{expected_turn_id!r}"
            )
        control_id = operation_id or self._mint_operation_id(participant_id, ControlKind.STEER)
        if pre_reserved:
            self._require_public_reservation(
                control_id,
                participant_id=participant_id,
                kind=ControlKind.STEER,
                phase=ControlDeliveryPhase.RESERVED,
                route=route,
                job_handle=job.handle,
            )
        else:
            self._provider.reserve(
                control_id,
                route,
                participant_id=participant_id,
                kind=ControlKind.STEER,
                phase=ControlDeliveryPhase.RESERVED,
                job_handle=job.handle,
                payload=prompt,
            )
            self._notify_reserved(on_reserved, control_id, job.handle)
        result = await self._provider.deliver(
            route,
            capability=RuntimeCapability.STEER,
            kind=ControlKind.STEER,
            participant_id=participant_id,
            control_operation_id=control_id,
            callback_operation_id=callback_operation_id or control_id,
            action={"kind": "paste_text", "text": prompt},
            job_handle=None,
        )
        return job, _delivery_label(result)

    @staticmethod
    def _steer_route_refusal(participant_id: str, route: ControlRoute) -> BadRequest:
        if not route.native_wiring:
            return BadRequest(
                f"steering participant {participant_id!r} requires native runtime wiring; "
                "its harness has no runtime, so the prompt can only be sent with the "
                "ordinary idle-guarded send or queued as a followup"
            )
        return BadRequest(
            f"steering participant {participant_id!r} is unavailable on its selected transport"
        )

    @staticmethod
    def _require_expected_turn(
        participant_id: str, actual: str | None, expected: str | None
    ) -> str:
        if actual is None:
            raise StaleTarget(f"participant {participant_id!r} has no active native turn to steer")
        if expected is not None and expected != actual:
            raise StaleTarget(
                f"participant {participant_id!r} is on native turn {actual!r}, "
                f"not expected turn {expected!r}"
            )
        return actual

    # ---- queued followups -------------------------------------------------

    async def queue_followup(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None = None,
        operation_id: str | None = None,
        callback_operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
        pre_reserved: bool = False,
    ) -> Job:
        """Create an awaitable send job immediately and reserve its queue slot."""
        with self._control_latency(ControlKind.QUEUE_FOLLOWUP, participant_id) as latency:
            job, transport = await self._queue_followup(
                participant_id,
                caller_id=caller_id,
                prompt=prompt,
                response_format=response_format,
                operation_id=operation_id,
                callback_operation_id=callback_operation_id,
                on_reserved=on_reserved,
                actor_client_id=actor_client_id,
                actor_participant_id=actor_participant_id,
                pre_reserved=pre_reserved,
            )
            # The queue accepted the item; its delivery is observed when a
            # dispatch pass delivers it, never optimistically here.
            latency.delivery = CONTROL_DELIVERY_QUEUED
            latency.transport = transport
            return job

    async def _queue_followup(  # noqa: PLR0912, PLR0915
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
        operation_id: str | None,
        callback_operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        actor_client_id: str | None,
        actor_participant_id: str | None,
        pre_reserved: bool,
    ) -> tuple[Job, str]:
        """The queue-followup body; returns its job and the reserved transport."""
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_QUEUE_FOLLOWUP)
            if pre_reserved:
                if operation_id is None:
                    raise RuntimeError("a pre-reserved public followup requires its control ID")
                existing = self._store.get_control_operation(operation_id)
                if (
                    existing is not None
                    and existing.delivery_phase is ControlDeliveryPhase.SETTLED
                    and existing.job_handle is not None
                ):
                    return self._require_job(existing.job_handle), existing.transport.value
            await self._gates.require_absent(participant_id)
            self._gates.check_prompt(prompt)
            pending = self._store.queued_control_operation_count(participant_id)
            queue_full = (
                pending > CONTROL_QUEUE_MAX_PENDING
                if pre_reserved
                else pending >= CONTROL_QUEUE_MAX_PENDING
            )
            if queue_full:
                raise Busy(
                    f"participant {participant_id!r} already holds {pending} queued "
                    f"followups (bound {CONTROL_QUEUE_MAX_PENDING}); await or "
                    "interrupt the pending handles before queueing another"
                )
            route = self.route_for(participant_id, RuntimeCapability.QUEUE_FOLLOWUP)
            if route.transport is None:
                raise BadRequest(
                    f"participant {participant_id!r} does not offer a followup transport"
                )
            # Pin native queued work to its reserved generation/session; never replay across backend
            # relaunches. Legacy queue entries deliberately carry no live-runtime identity.
            runtime = self._runtime_for(participant_id)
            generation: int | None = None
            session: str | None = None
            predecessor: str | None = None
            if route.is_provider:
                self._provider.require(participant_id, RuntimeCapability.QUEUE_FOLLOWUP, route)
            elif route.is_native and runtime is not None:
                snapshot = await self._snapshot_for_control(runtime, participant_id)
                route = self._require_current_native_route(
                    participant_id,
                    RuntimeCapability.QUEUE_FOLLOWUP,
                    snapshot,
                    require_available=False,
                )
                # The queue is Theater-owned, so the QUEUE_FOLLOWUP capability (forbidden native
                # thread/queue use) never gates it.
                self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
                generation = snapshot.backend_generation
                session = snapshot.native_session_id
                predecessor = self._queue_predecessor(participant_id, snapshot)
            elif route.is_native:
                raise self._disconnected_native_refusal(participant_id, "queue_followup")
            self._gates.check_absent(participant_id)
            if pre_reserved:
                if operation_id is None:
                    raise RuntimeError("a pre-reserved public followup requires its control ID")
                reserved = self._require_public_reservation(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.QUEUE_FOLLOWUP,
                    phase=ControlDeliveryPhase.QUEUED,
                    route=route,
                )
                if reserved.job_handle is None:
                    raise RuntimeError("a queued public control requires its durable job")
                if route.is_native and runtime is not None:
                    self._require_reserved_native_identity(reserved, snapshot)
                    payload = self._queue_payload(
                        predecessor=predecessor,
                        callback_operation_id=callback_operation_id,
                    )
                    self._store.set_queued_control_payload(operation_id, payload or "{}")
                job = self._require_job(reserved.job_handle)
                transport = reserved.transport
                self.schedule_dispatch(participant_id)
                return job, transport.value
            with self._store.write_unit() as unit:
                connection = unit.connection
                sequence = self._store.allocate_control_queue_sequence(connection=connection)
                handle = f"{participant_id}#{sequence}"
                assert route.transport is not None
                transport = route.transport
                control_id = operation_id or f"{handle}:{ControlKind.QUEUE_FOLLOWUP.value}"
                payload = self._queue_payload(
                    predecessor=predecessor,
                    callback_operation_id=callback_operation_id,
                )
                if route.is_provider:
                    self._provider.reserve(
                        control_id,
                        route,
                        participant_id=participant_id,
                        kind=ControlKind.QUEUE_FOLLOWUP,
                        phase=ControlDeliveryPhase.QUEUED,
                        job_handle=handle,
                        queue_sequence=sequence,
                        payload=payload,
                        connection=connection,
                    )
                else:
                    self._reserve(
                        control_id,
                        participant_id=participant_id,
                        kind=ControlKind.QUEUE_FOLLOWUP,
                        transport=transport,
                        phase=ControlDeliveryPhase.QUEUED,
                        job_handle=handle,
                        backend_generation=generation,
                        native_session_id=session,
                        queue_sequence=sequence,
                        payload=payload,
                        connection=connection,
                    )
                reserved = self._store.get_control_operation(control_id, connection=connection)
                assert reserved is not None
                event = control_event(
                    self._store,
                    reserved,
                    connection,
                    revision=next_revision(self._store, connection),
                )
                if event is not None:
                    self._store.journal.append_group(unit, [event])
            self._jobs.create(
                handle=handle,
                caller_id=caller_id,
                target_id=participant_id,
                kind="send",
                prompt=prompt,
                cwd=None,
                response_format=response_format,
                actor_client_id=actor_client_id,
                actor_participant_id=actor_participant_id,
            )
            self._notify_reserved(on_reserved, control_id, handle)
            job = self._require_job(handle)
        # Already idle? Dispatch on the next scheduling opportunity.
        self.schedule_dispatch(participant_id)
        return job, transport.value

    def schedule_dispatch(self, participant_id: str) -> None:
        """Try to dispatch the queue head on the next scheduling opportunity."""
        self._schedule_maintenance(participant_id)
        if self._closing:
            return
        existing = self._dispatch_tasks.get(participant_id)
        if existing is not None and not existing.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._dispatch_pass_logged(participant_id))
        self._dispatch_tasks[participant_id] = task

    async def _dispatch_pass_logged(self, participant_id: str) -> QueueDispatchOutcome:
        """Run one dispatch pass so its crash is logged, never unretrieved."""
        try:
            outcome = await self.dispatch_queue(participant_id)
        except Exception:
            logger.exception(
                "queue dispatch pass for %s crashed; no retry — a later "
                "scheduling opportunity will run another pass",
                participant_id,
            )
            return QueueDispatchOutcome()
        else:
            if self._scheduler_started and self._has_maintenance_work(participant_id):
                self._schedule_maintenance(participant_id)
            return outcome

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

    async def dispatch_queue(self, participant_id: str) -> QueueDispatchOutcome:
        """Dispatch queue items one at a time, after an authoritative idle check."""
        dispatched: list[str] = []
        failed: list[tuple[str, str]] = []
        deferred = False
        async with self._lock(participant_id):
            while True:
                queued = self._store.queued_control_operations(participant_id)
                head_id = queued[0].operation_id if queued else None
                outcome = await self._dispatch_head(participant_id)
                dispatched.extend(outcome.dispatched)
                failed.extend(outcome.failed)
                if outcome.deferred:
                    deferred = True
                if head_id is not None and (outcome.dispatched or outcome.failed):
                    self._control_notifier.notify(head_id)
                if not outcome.dispatched and not outcome.failed:
                    break
                # A definitive failure removed one item; try the next.
                if outcome.dispatched:
                    break
        return QueueDispatchOutcome(
            dispatched=tuple(dispatched), failed=tuple(failed), deferred=deferred
        )

    async def _dispatch_head(self, participant_id: str) -> QueueDispatchOutcome:
        queued = self._store.queued_control_operations(participant_id)
        if not queued:
            return QueueDispatchOutcome()
        head = queued[0]
        job = self._store.get_job(head.job_handle) if head.job_handle else None
        if job is None or job.state != JobState.RUNNING:
            # The job vanished before dispatch (crash residue); the queue
            # slot is definitively unanswerable.
            self._store.settle_control_operation(
                head.operation_id,
                result=DeliveryResult.REJECTED,
                error_code="job_missing",
                error=f"queued job {head.job_handle!r} is no longer running",
                updated_at=self._clock(),
            )
            return QueueDispatchOutcome(failed=((head.job_handle or "", "job_missing"),))
        caller_id = job.caller_id
        if caller_id is None:
            return self._fail_queued_item(
                head,
                job,
                BadRequest(f"queued job {job.handle!r} has no caller identity"),
            )
        try:
            self._gates.authorize(participant_id, caller_id, ACTION_QUEUE_DISPATCH)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        try:
            await self._gates.require_absent(participant_id)
            await self._gates.send_preflight(participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        except Exception as exc:
            # A preflight failure never fails a protected queue: recheck
            # presence first; an unprotected head classifies as before.
            try:
                await self._gates.require_absent(participant_id)
            except Exception:
                logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
                return QueueDispatchOutcome(deferred=True)
            return self._fail_queued_item(head, job, exc)
        route = self.route_for(participant_id, RuntimeCapability.QUEUE_FOLLOWUP)
        if route.transport is None:
            return self._fail_queued_item(
                head,
                job,
                BadRequest(f"participant {participant_id!r} no longer offers a followup transport"),
            )
        runtime = self._runtime_for(participant_id)
        if head.provider_id is not None:
            if not route.is_provider:
                return self._fail_queued_item(
                    head,
                    job,
                    StaleTarget(
                        f"provider route for participant {participant_id!r} changed before "
                        "queued delivery; the prompt is never failed over"
                    ),
                )
            return await self._dispatch_head_provider(participant_id, head, job, route)
        if route.is_native:
            if runtime is not None:
                return await self._dispatch_head_native(runtime, participant_id, head, job)
            logger.info(
                "queued followup %s of %s deferred: the natively-wired "
                "participant's runtime is not connected; no legacy pane delivery",
                head.operation_id,
                participant_id,
            )
            return QueueDispatchOutcome(deferred=True)
        return await self._dispatch_head_legacy(participant_id, head, job, caller_id)

    async def _dispatch_head_provider(
        self,
        participant_id: str,
        head: ControlOperation,
        job: Job,
        route: ControlRoute,
    ) -> QueueDispatchOutcome:
        try:
            terminal = self._provider.require(
                participant_id, RuntimeCapability.QUEUE_FOLLOWUP, route
            )
        except TEMPORARY_REFUSALS as exc:
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        pinned = (
            head.provider_id,
            head.provider_generation,
            head.terminal_id,
            head.terminal_incarnation,
        )
        current = (
            terminal.provider_id,
            terminal.provider_generation,
            terminal.terminal_id,
            terminal.terminal_incarnation,
        )
        if pinned != current:
            return self._fail_queued_item(
                head,
                job,
                StaleTarget(
                    f"provider terminal identity for participant {participant_id!r} changed; "
                    "the queued prompt is never replayed or failed over"
                ),
            )
        if self._store.has_execution_barrier(participant_id):
            return QueueDispatchOutcome(deferred=True)
        queued_handles = {
            operation.job_handle
            for operation in self._store.queued_control_operations(participant_id)
            if operation.job_handle is not None
        }
        if any(
            candidate.handle not in queued_handles
            for candidate in self._store.running_jobs_for_target(participant_id)
        ):
            return QueueDispatchOutcome(deferred=True)
        callback_operation_id = self._callback_operation_id(head) or head.operation_id
        result = await self._provider.deliver(
            route,
            capability=RuntimeCapability.QUEUE_FOLLOWUP,
            kind=ControlKind.QUEUE_FOLLOWUP,
            participant_id=participant_id,
            control_operation_id=head.operation_id,
            callback_operation_id=callback_operation_id,
            action={"kind": "submit_text", "text": job.prompt or ""},
            job_handle=job.handle,
        )
        if result is DeliveryResult.REJECTED:
            return QueueDispatchOutcome(failed=((job.handle, SEND_REJECTED_ERROR_CODE),))
        return QueueDispatchOutcome(dispatched=(job.handle,))

    async def _dispatch_head_legacy(
        self,
        participant_id: str,
        head: ControlOperation,
        job: Job,
        caller_id: str,
    ) -> QueueDispatchOutcome:
        if head.transport is not ControlTransport.LEGACY_TMUX:
            selected = self._select_queued_route(
                head,
                transport=ControlTransport.LEGACY_TMUX,
                backend_generation=None,
                native_session_id=None,
                payload=None,
            )
            if selected is None:
                return QueueDispatchOutcome(deferred=True)
            head = selected
        try:
            await self._gates.require_absent(participant_id)
            await self._gates.legacy_copy_mode_check(participant_id)
            # Recheck after the awaited copy-mode query, before dispatch effects.
            await self._gates.require_absent(participant_id)
            # The busy/claim check mutates claim rows; it runs after all awaited
            # prep, its synchronous body adjacent to dispatch initiation.
            await self._gates.legacy_busy_check(participant_id)
        except TEMPORARY_REFUSALS as exc:
            # Legacy busy defers the unchanged FIFO head until active work settles.
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        self._store.mark_control_operation_dispatched(head.operation_id, updated_at=self._clock())
        try:
            await self._gates.legacy_deliver(participant_id, job.prompt or "")
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        self._store.settle_control_operation(
            head.operation_id,
            result=DeliveryResult.ACCEPTED,
            updated_at=self._clock(),
        )
        self._record_legacy_send(
            participant_id,
            caller_id=caller_id,
            job=job,
            prompt=job.prompt or "",
        )
        return QueueDispatchOutcome(dispatched=(job.handle,))

    async def _dispatch_head_native(
        self,
        runtime: HarnessRuntime,
        participant_id: str,
        head: ControlOperation,
        job: Job,
    ) -> QueueDispatchOutcome:
        # Protected heads stay queued, including protection acquired during the snapshot.
        try:
            snapshot = await self._snapshot_for_control(runtime, participant_id)
        except TEMPORARY_REFUSALS as exc:
            logger.debug("queued followup %s deferred: %s", head.operation_id, exc)
            return QueueDispatchOutcome(deferred=True)
        try:
            route = self._require_current_native_route(
                participant_id,
                RuntimeCapability.QUEUE_FOLLOWUP,
                snapshot,
                require_available=False,
            )
            # Native delivery needs SEND: the followup queue is Theater-owned, and QUEUE_FOLLOWUP
            # marks forbidden native queue use, so it never gates this path.
            self._require_capability(participant_id, snapshot, RuntimeCapability.SEND, "send")
        except Exception as exc:
            return self._fail_queued_item(head, job, exc)
        if not route.route_available:
            logger.debug(
                "queued followup %s deferred: its exact native route is disconnected",
                head.operation_id,
            )
            return QueueDispatchOutcome(deferred=True)
        # ``UNKNOWN``/disconnected/missing identity are not idle.
        if not self._is_authoritatively_idle(snapshot):
            predecessor = self._queue_predecessor(participant_id, snapshot)
            if predecessor is not None:
                # A human can start another turn while Theater's FIFO is pending.
                self._bind_queued_predecessor(participant_id, snapshot, predecessor)
            return QueueDispatchOutcome(deferred=True)
        self._clear_execution_barriers_from_idle_snapshot(participant_id, snapshot)
        if self._store.has_execution_barrier(participant_id):
            return QueueDispatchOutcome(deferred=True)
        if self._store.active_running_jobs_for_target(participant_id):
            return QueueDispatchOutcome(deferred=True)
        if head.transport is not ControlTransport.NATIVE_RUNTIME:
            selected = self._select_queued_route(
                head,
                transport=ControlTransport.NATIVE_RUNTIME,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                payload=None,
            )
            if selected is None:
                return QueueDispatchOutcome(deferred=True)
            head = selected
        if (
            head.backend_generation is None
            or head.native_session_id is None
            or head.backend_generation != snapshot.backend_generation
            or head.native_session_id != snapshot.native_session_id
        ):
            return self._fail_queued_item(
                head,
                job,
                StaleTarget(
                    f"the native session of {participant_id!r} changed "
                    f"(reserved generation/session {head.backend_generation}/"
                    f"{head.native_session_id!r}, now {snapshot.backend_generation}/"
                    f"{snapshot.native_session_id!r}); the queued followup is never "
                    "replayed into another session"
                ),
            )
        cwd = self._gates.cwd_for(participant_id)
        if cwd is not None:
            self._jobs.attach_touch_accumulator(job.handle, cwd=cwd)
        await self._deliver_native(
            runtime,
            kind=ControlKind.QUEUE_FOLLOWUP,
            participant_id=participant_id,
            operation_id=head.operation_id,
            prompt=job.prompt or "",
            job_handle=job.handle,
            snapshot=snapshot,
        )
        return QueueDispatchOutcome(dispatched=(job.handle,))

    def _fail_queued_item(
        self, operation: ControlOperation, job: Job, exc: Exception
    ) -> QueueDispatchOutcome:
        """Finish one queued item with an explicit error; never replay it."""
        error_code = _error_code_of(exc)
        self._store.settle_control_operation(
            operation.operation_id,
            result=DeliveryResult.REJECTED,
            error_code=error_code,
            error=str(exc),
            updated_at=self._clock(),
        )
        self._jobs.finish(
            job.handle,
            state=JobState.CRASHED,
            result=str(exc),
            error_code=error_code,
        )
        logger.info(
            "queued followup %s for job %s failed at dispatch (%s): %s",
            operation.operation_id,
            job.handle,
            error_code,
            exc,
        )
        return QueueDispatchOutcome(failed=((job.handle, error_code),))

    def _select_queued_route(
        self,
        operation: ControlOperation,
        *,
        transport: ControlTransport,
        backend_generation: int | None,
        native_session_id: str | None,
        payload: str | None,
    ) -> ControlOperation | None:
        if not self._store.set_queued_control_route(
            operation.operation_id,
            transport=transport,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            payload=payload,
            updated_at=self._clock(),
        ):
            return None
        selected = self._store.get_control_operation(operation.operation_id)
        return (
            selected
            if selected is not None and selected.delivery_phase is ControlDeliveryPhase.QUEUED
            else None
        )

    # ---- settings ---------------------------------------------------------

    async def update_settings(
        self,
        participant_id: str,
        *,
        caller_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        pre_reserved: bool = False,
    ) -> SettingsOutcome:
        """Idle-only model/reasoning update, capability- and allowlist-gated."""
        with self._control_latency(ControlKind.SETTINGS_UPDATE, participant_id) as latency:
            outcome = await self._update_settings(
                participant_id,
                caller_id=caller_id,
                model=model,
                reasoning_effort=reasoning_effort,
                operation_id=operation_id,
                on_reserved=on_reserved,
                pre_reserved=pre_reserved,
            )
            # ``applied`` is True only after native confirmation/readback,
            # False on a definitive refusal, None while uncertain.
            if outcome.applied:
                latency.delivery = CONTROL_DELIVERY_ACCEPTED
            elif outcome.applied is not None:
                latency.delivery = CONTROL_DELIVERY_REJECTED
            else:
                latency.delivery = CONTROL_DELIVERY_UNKNOWN
            # A settings update can only complete over the native runtime;
            # the body established that fact, so no extra read is needed.
            latency.transport = ControlTransport.NATIVE_RUNTIME.value
            return outcome

    async def _update_settings(  # noqa: PLR0912, PLR0915
        self,
        participant_id: str,
        *,
        caller_id: str,
        model: str | None,
        reasoning_effort: str | None,
        operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        pre_reserved: bool,
    ) -> SettingsOutcome:
        """The settings-update body."""
        if model is None and reasoning_effort is None:
            raise BadRequest(
                f"nothing to update for participant {participant_id!r}: supply "
                "model and/or reasoning_effort; approval and sandbox policy are "
                "never changed here"
            )
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_SETTINGS_UPDATE)
            await self._gates.require_absent(participant_id)
            self._gates.check_settings(model, reasoning_effort)
            route = self.route_for(participant_id, RuntimeCapability.SETTINGS_UPDATE)
            if not route.is_native:
                if not route.native_wiring:
                    raise BadRequest(
                        f"settings updates for participant {participant_id!r} require native "
                        "runtime wiring; its harness has no runtime, so the model is "
                        "fixed at launch"
                    )
                raise BadRequest(
                    f"settings updates for participant {participant_id!r} are unavailable on "
                    "its selected transport"
                )
            if runtime is None:
                raise self._disconnected_native_refusal(participant_id, "settings update")
            snapshot = await self._snapshot_for_control(runtime, participant_id)
            route = self._require_current_native_route(
                participant_id, RuntimeCapability.SETTINGS_UPDATE, snapshot
            )
            if not snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE):
                reason = snapshot.capabilities.reason_for(RuntimeCapability.SETTINGS_UPDATE)
                raise BadRequest(
                    f"participant {participant_id!r} does not support settings "
                    f"updates ({reason}); the installed native API gates this "
                    "capability, so the model stays as configured at launch"
                )
            self._require_supported_settings(participant_id, snapshot, model, reasoning_effort)
            self._reject_busy(participant_id, snapshot, operation=BusyOperation.SETTINGS)
            payload = json.dumps(
                {
                    key: value
                    for key, value in (
                        ("model", model),
                        ("reasoning_effort", reasoning_effort),
                    )
                    if value is not None
                }
            )
            operation_id = operation_id or self._mint_operation_id(
                participant_id, ControlKind.SETTINGS_UPDATE
            )
            if pre_reserved:
                reserved = self._require_public_reservation(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.SETTINGS_UPDATE,
                    phase=ControlDeliveryPhase.RESERVED,
                    route=route,
                )
                self._require_reserved_native_identity(reserved, snapshot)
            else:
                self._reserve(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.SETTINGS_UPDATE,
                    transport=ControlTransport.NATIVE_RUNTIME,
                    phase=ControlDeliveryPhase.RESERVED,
                    backend_generation=snapshot.backend_generation,
                    native_session_id=snapshot.native_session_id,
                    payload=payload,
                )
                self._notify_reserved(on_reserved, operation_id, None)
            self._store.mark_control_operation_dispatched(
                operation_id,
                native_session_id=snapshot.native_session_id,
                updated_at=self._clock(),
            )
            try:
                receipt = await runtime.update_settings(
                    operation_id=operation_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
                )
            except asyncio.CancelledError:
                # Settings have no job completion obligation, but their native mutation may already
                # have crossed the transport boundary.
                self._settle_uncertain(
                    operation_id,
                    error=(
                        "the settings update was cancelled after transmission began; "
                        "its acknowledgement is unknown and it is never retried"
                    ),
                )
                self._count_unknown_delivery(ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_ACK_LOST)
                raise
            except Exception as exc:
                # No Theater job hangs on a settings operation; record the
                # uncertainty so the row is honestly settled and prunable.
                self._store.settle_control_operation(
                    operation_id,
                    result=DeliveryResult.UNKNOWN,
                    error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                    error=str(exc),
                    updated_at=self._clock(),
                )
                logger.warning("settings update for %s is uncertain: %s", participant_id, exc)
                self._count_unknown_delivery(ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_ACK_LOST)
                return SettingsOutcome(applied=None, model=model, reasoning_effort=reasoning_effort)
            if not self._receipt_names_operation(operation_id, receipt):
                self._settle_uncertain(
                    operation_id,
                    error=(
                        f"the settings receipt named operation {receipt.operation_id!r}, "
                        f"not {operation_id!r}; the update is uncertain and the "
                        "receipt is not trusted to settle it"
                    ),
                )
                self._count_unknown_delivery(
                    ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_RECEIPT_MISMATCH
                )
                return SettingsOutcome(
                    applied=None,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                    error="the settings update stayed uncertain: the native receipt "
                    "named a different operation",
                )
            self._settle_from_receipt(operation_id, receipt)
            if receipt.result is DeliveryResult.REJECTED:
                return SettingsOutcome(
                    applied=False,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    error_code=receipt.error_code,
                    error=receipt.error,
                )
            if receipt.result is DeliveryResult.UNKNOWN:
                logger.warning("settings update for %s stayed uncertain", participant_id)
                self._count_unknown_delivery(
                    ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_RECEIPT_UNKNOWN
                )
                return SettingsOutcome(applied=None, model=model, reasoning_effort=reasoning_effort)
            # Effective values only after native confirmation/readback.
            try:
                fresh = await runtime.snapshot()
                self._gates.record_native_snapshot(participant_id, runtime, fresh)
            except Exception as exc:
                logger.warning(
                    "settings update for %s was accepted but the effective-value "
                    "readback failed: %s; the application stays uncertain",
                    participant_id,
                    exc,
                )
                self._count_unknown_delivery(
                    ControlKind.SETTINGS_UPDATE, CONTROL_UNKNOWN_READBACK_FAILED
                )
                return SettingsOutcome(
                    applied=None,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                    error=(
                        "the native backend accepted the settings update, but the "
                        "effective-value readback failed; whether the application "
                        "took effect is unknown"
                    ),
                )
            return SettingsOutcome(
                applied=True,
                model=fresh.settings.model,
                reasoning_effort=fresh.settings.reasoning_effort,
            )

    @staticmethod
    def _require_supported_settings(
        participant_id: str,
        snapshot: RuntimeSnapshot,
        model: str | None,
        reasoning_effort: str | None,
    ) -> None:
        requested_fields = {
            field_name
            for field_name, value in (
                (RuntimeSettingField.MODEL, model),
                (RuntimeSettingField.REASONING_EFFORT, reasoning_effort),
            )
            if value is not None
        }
        unsupported_fields = requested_fields - snapshot.settings.supported_fields
        if unsupported_fields:
            unsupported = ", ".join(sorted(str(field) for field in unsupported_fields))
            raise BadRequest(
                f"participant {participant_id!r} does not support updating settings "
                f"field(s): {unsupported}; no native mutation was attempted"
            )

    # ---- interrupt --------------------------------------------------------

    async def interrupt(
        self,
        participant_id: str,
        *,
        caller_id: str,
        operation_id: str | None = None,
        callback_operation_id: str | None = None,
        on_reserved: Callable[[str, str | None], None] | None = None,
        pre_reserved: bool = False,
    ) -> InterruptOutcome:
        """Cancel every undelivered followup, then interrupt the active turn."""
        with self._control_latency(ControlKind.INTERRUPT, participant_id) as latency:
            outcome = await self._interrupt(
                participant_id,
                caller_id=caller_id,
                operation_id=operation_id,
                callback_operation_id=callback_operation_id,
                on_reserved=on_reserved,
                pre_reserved=pre_reserved,
            )
            # Only an accepted receipt confirms interruption; UNKNOWN remains delivery_unknown.
            latency.delivery = (
                CONTROL_DELIVERY_ACCEPTED
                if outcome.interrupted
                else (
                    CONTROL_DELIVERY_UNKNOWN
                    if outcome.reason == DELIVERY_UNKNOWN_ERROR_CODE
                    else CONTROL_DELIVERY_REJECTED
                )
            )
            route = self.route_for(participant_id, RuntimeCapability.INTERRUPT)
            latency.transport = (
                route.transport.value if route.transport else CONTROL_TRANSPORT_UNKNOWN
            )
            return outcome

    async def _interrupt(  # noqa: PLR0912, PLR0915
        self,
        participant_id: str,
        *,
        caller_id: str,
        operation_id: str | None,
        callback_operation_id: str | None,
        on_reserved: Callable[[str, str | None], None] | None,
        pre_reserved: bool,
    ) -> InterruptOutcome:
        """The interrupt body."""
        runtime = self._runtime_for(participant_id)
        async with self._lock(participant_id):
            self._gates.authorize(participant_id, caller_id, ACTION_INTERRUPT)
            route = self.route_for(participant_id, RuntimeCapability.INTERRUPT)
            await self._require_absent(participant_id, route)
            if route.is_provider:
                self._provider.require(participant_id, RuntimeCapability.INTERRUPT, route)
                cancelled = await self._cancel_queued_followups(participant_id)
                action = interrupt_action(self._store, self._gates, participant_id)
                if action is None:
                    if pre_reserved and operation_id is not None:
                        self._store.settle_control_operation(
                            operation_id,
                            result=DeliveryResult.ACCEPTED,
                            error_code="already_idle",
                            error="participant had no active work to interrupt",
                            updated_at=self._clock(),
                        )
                    return InterruptOutcome(
                        interrupted=False, reason="already_idle", cancelled_followups=cancelled
                    )
                control_id = operation_id or self._mint_operation_id(
                    participant_id, ControlKind.INTERRUPT
                )
                if pre_reserved:
                    self._require_public_reservation(
                        control_id,
                        participant_id=participant_id,
                        kind=ControlKind.INTERRUPT,
                        phase=ControlDeliveryPhase.RESERVED,
                        route=route,
                    )
                else:
                    self._provider.reserve(
                        control_id,
                        route,
                        participant_id=participant_id,
                        kind=ControlKind.INTERRUPT,
                        phase=ControlDeliveryPhase.RESERVED,
                    )
                    self._notify_reserved(on_reserved, control_id, None)
                result = await self._provider.deliver(
                    route,
                    capability=RuntimeCapability.INTERRUPT,
                    kind=ControlKind.INTERRUPT,
                    participant_id=participant_id,
                    control_operation_id=control_id,
                    callback_operation_id=callback_operation_id or control_id,
                    action=action,
                    job_handle=None,
                )
                return InterruptOutcome(
                    interrupted=result is DeliveryResult.ACCEPTED,
                    reason=(
                        None
                        if result is DeliveryResult.ACCEPTED
                        else DELIVERY_UNKNOWN_ERROR_CODE
                        if result is DeliveryResult.UNKNOWN
                        else "refused"
                    ),
                    cancelled_followups=cancelled,
                )
            if not route.is_native:
                if not route.native_wiring:
                    raise BadRequest(
                        f"interrupting participant {participant_id!r} through the control "
                        "service requires native runtime wiring; its harness uses the "
                        "existing pane-interrupt path"
                    )
                raise BadRequest(
                    f"interrupting participant {participant_id!r} is unavailable on its selected "
                    "transport"
                )
            if runtime is None:
                raise self._disconnected_native_refusal(participant_id, "interrupt")
            snapshot = await self._snapshot_for_control(runtime, participant_id)
            route = self._require_current_native_route(
                participant_id, RuntimeCapability.INTERRUPT, snapshot
            )
            self._require_capability(
                participant_id, snapshot, RuntimeCapability.INTERRUPT, "interruption"
            )
            cancelled = await self._cancel_queued_followups(participant_id)
            turn = snapshot.native_turn_id
            if turn is None:
                if pre_reserved and operation_id is not None:
                    self._store.settle_control_operation(
                        operation_id,
                        result=DeliveryResult.ACCEPTED,
                        error_code="already_idle",
                        error="participant had no active native turn to interrupt",
                        updated_at=self._clock(),
                    )
                return InterruptOutcome(
                    interrupted=False, reason="already_idle", cancelled_followups=cancelled
                )
            operation_id = operation_id or self._mint_operation_id(
                participant_id, ControlKind.INTERRUPT
            )
            if pre_reserved:
                reserved = self._require_public_reservation(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.INTERRUPT,
                    phase=ControlDeliveryPhase.RESERVED,
                    route=route,
                )
                self._require_reserved_native_identity(reserved, snapshot)
            else:
                self._reserve(
                    operation_id,
                    participant_id=participant_id,
                    kind=ControlKind.INTERRUPT,
                    transport=ControlTransport.NATIVE_RUNTIME,
                    phase=ControlDeliveryPhase.RESERVED,
                    backend_generation=snapshot.backend_generation,
                    native_session_id=snapshot.native_session_id,
                    native_turn_id=turn,
                )
                self._notify_reserved(on_reserved, operation_id, None)
            self._store.mark_control_operation_dispatched(
                operation_id,
                native_session_id=snapshot.native_session_id,
                native_turn_id=turn,
                updated_at=self._clock(),
            )
            try:
                receipt = await runtime.interrupt(operation_id=operation_id, native_turn_id=turn)
            except asyncio.CancelledError:
                # The exact interrupt may have reached the backend, but no job state follows from
                # this receipt path.
                self._settle_uncertain(
                    operation_id,
                    error=(
                        "the interruption control was cancelled after transmission began; "
                        "its acknowledgement is unknown and it is never retried"
                    ),
                )
                self._count_unknown_delivery(ControlKind.INTERRUPT, CONTROL_UNKNOWN_ACK_LOST)
                raise
            except Exception as exc:
                self._store.settle_control_operation(
                    operation_id,
                    result=DeliveryResult.UNKNOWN,
                    error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                    error=str(exc),
                    updated_at=self._clock(),
                )
                logger.warning(
                    "interrupt delivery for %s turn %s is uncertain: %s",
                    participant_id,
                    turn,
                    exc,
                )
                self._count_unknown_delivery(ControlKind.INTERRUPT, CONTROL_UNKNOWN_ACK_LOST)
                return InterruptOutcome(
                    interrupted=False,
                    reason=DELIVERY_UNKNOWN_ERROR_CODE,
                    cancelled_followups=cancelled,
                )
            if not self._receipt_names_operation(operation_id, receipt):
                self._settle_uncertain(
                    operation_id,
                    error=(
                        f"the interrupt receipt named operation {receipt.operation_id!r}, "
                        f"not {operation_id!r}; the interruption is uncertain and the "
                        "receipt is not trusted to settle it"
                    ),
                )
                self._count_unknown_delivery(
                    ControlKind.INTERRUPT, CONTROL_UNKNOWN_RECEIPT_MISMATCH
                )
                return InterruptOutcome(
                    interrupted=False,
                    reason=DELIVERY_UNKNOWN_ERROR_CODE,
                    cancelled_followups=cancelled,
                )
            self._settle_from_receipt(operation_id, receipt)
            if receipt.result is not DeliveryResult.ACCEPTED:
                return InterruptOutcome(
                    interrupted=False,
                    reason=receipt.error_code or "refused",
                    cancelled_followups=cancelled,
                )
            return InterruptOutcome(interrupted=True, cancelled_followups=cancelled)

    async def handle_native_ui_interrupt(
        self, participant_id: str, *, native_turn_id: str | None = None
    ) -> tuple[str, ...]:
        """A native-UI-initiated interruption: cancel the pending queue."""
        del native_turn_id  # the exact turn is already gone; nothing to request
        return await self.cancel_queued_followups(participant_id)

    async def cancel_queued_followups(self, participant_id: str) -> tuple[str, ...]:
        """Cancel every undelivered queued followup; return the cancelled handles.

        The queue is Theater-owned, so every transport's cancel ends ``killed``/``interrupted`` and
        the post-interrupt idle transition finds nothing left to dispatch.
        """
        async with self._lock(participant_id):
            return await self._cancel_queued_followups(participant_id)

    @asynccontextmanager
    async def hold_participant_locks(self, participant_ids: Iterable[str]) -> AsyncIterator[None]:
        """Hold participant schedulers in stable order for an atomic batch."""
        locks = [self._lock(participant_id) for participant_id in sorted(set(participant_ids))]
        acquired: list[asyncio.Lock] = []
        try:
            for lock in locks:
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    def cancel_queued_for_control_transfer(
        self,
        participant_ids: Sequence[str],
        *,
        unit,
        timestamp: float,
    ) -> tuple[Job, ...]:
        """Cancel only undispatched followups inside the ownership write unit."""
        cancelled: list[Job] = []
        for participant_id in participant_ids:
            operations = self._store.queued_control_operations(
                participant_id, connection=unit.connection
            )
            for operation in operations:
                self._store.settle_control_operation(
                    operation.operation_id,
                    result=DeliveryResult.REJECTED,
                    error_code=CONTROL_TRANSFERRED_ERROR_CODE,
                    error=(
                        "queued followup was cancelled before dispatch because control "
                        "ownership changed"
                    ),
                    updated_at=timestamp,
                    connection=unit.connection,
                )
                unit.after_commit(
                    lambda operation_id=operation.operation_id: self.notify_persisted_settlement(
                        operation_id
                    )
                )
                if operation.job_handle is None:
                    continue
                job = self._store.get_job(operation.job_handle, connection=unit.connection)
                if job is None or job.state != JobState.RUNNING:
                    continue
                finished = replace(
                    job,
                    state=JobState.KILLED.value,
                    result=(
                        "Queued followup was cancelled before dispatch because control "
                        "ownership changed. Queue it again under the new owner if needed."
                    ),
                    error_code=CONTROL_TRANSFERRED_ERROR_CODE,
                    finished_at=timestamp,
                )
                self._store.finish_job(
                    job.handle,
                    state=finished.state,
                    result=finished.result,
                    error_code=finished.error_code,
                    finished_at=finished.finished_at,
                    response_format=finished.response_format,
                    structured_result=finished.structured_result,
                    structured_status=finished.structured_status,
                    connection=unit.connection,
                )
                unit.after_commit(
                    lambda handle=job.handle: self._jobs.finish(
                        handle,
                        state=JobState.KILLED,
                        error_code=CONTROL_TRANSFERRED_ERROR_CODE,
                    )
                )
                cancelled.append(finished)
        return tuple(cancelled)

    async def _cancel_queued_followups(self, participant_id: str) -> tuple[str, ...]:
        """Durably cancel every queued followup; return the cancelled handles."""
        return self._cancel_pending_followups(participant_id)

    def _cancel_pending_followups(
        self, participant_id: str, evidence: NativeTerminalEvidence | None = None
    ) -> tuple[str, ...]:
        """Cancel only the pending work causally covered by native evidence."""
        cancelled: list[str] = []
        for operation in self._store.queued_control_operations(participant_id):
            if evidence is not None and not self._interruption_covers(operation, evidence):
                continue
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.REJECTED,
                error_code=INTERRUPTED_ERROR_CODE,
                error="interrupted before dispatch; the active turn was interrupted",
                updated_at=self._clock(),
            )
            self._control_notifier.notify(operation.operation_id)
            handle = operation.job_handle or ""
            if operation.job_handle:
                self._jobs.finish(
                    operation.job_handle,
                    state=JobState.KILLED,
                    result=(
                        "Queued followup was cancelled by interrupt before it was "
                        "delivered; it was never sent to the participant. Queue it "
                        "again if the work is still wanted."
                    ),
                    error_code=INTERRUPTED_ERROR_CODE,
                )
                cancelled.append(handle)
        return tuple(cancelled)

    @staticmethod
    def _interruption_covers(operation: ControlOperation, evidence: NativeTerminalEvidence) -> bool:
        if (
            operation.transport is not ControlTransport.NATIVE_RUNTIME
            or operation.backend_generation != evidence.backend_generation
            or operation.native_session_id != evidence.native_session_id
        ):
            return False
        if not evidence.from_history:
            return True
        if evidence.completed_at is not None:
            if operation.created_at < evidence.completed_at:
                return True
            # Native history timestamps can have one-second precision.
            if operation.created_at >= evidence.completed_at + 1:
                return False
        if operation.payload is None:
            return False
        try:
            payload = json.loads(operation.payload)
        except (TypeError, ValueError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("queue_predecessor_turn") == evidence.native_turn_id
        )

    # ---- terminal evidence and completion ---------------------------------

    async def record_terminal_evidence(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        outcome: NativeTurnOutcome,
    ) -> Job | None:
        """Persist and process evidence under its participant's control lock."""
        async with self._lock(participant_id):
            return self._record_terminal_evidence_locked(
                participant_id, backend_generation=backend_generation, outcome=outcome
            )

    def _record_terminal_evidence_locked(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        outcome: NativeTurnOutcome,
    ) -> Job | None:
        """Persist terminal evidence, then finish exactly its mapped job."""
        evidence = NativeTerminalEvidence(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=outcome.native_session_id,
            native_turn_id=outcome.native_turn_id,
            terminal=outcome.terminal,
            result=outcome.result,
            completeness=outcome.completeness,
            provenance=outcome.provenance,
            error_code=outcome.error_code,
            error=outcome.error,
            recorded_at=self._clock(),
            from_history=outcome.from_history,
            completed_at=outcome.completed_at,
        )
        first_write = self._store.record_native_terminal_evidence(evidence)
        if not first_write:
            # Stored evidence already exists for this exact turn: it wins.
            persisted = self._store.get_native_terminal_evidence(
                participant_id=participant_id,
                backend_generation=backend_generation,
                native_session_id=outcome.native_session_id,
                native_turn_id=outcome.native_turn_id,
            )
            if persisted is not None:
                evidence = persisted
                logger.warning(
                    "duplicate terminal evidence for %s turn %s conflicts with the "
                    "persisted first write; finishing from the persisted evidence",
                    participant_id,
                    outcome.native_turn_id,
                )
        operation = self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=outcome.native_session_id,
            native_turn_id=outcome.native_turn_id,
        )
        job: Job | None = None
        current = (
            self._store.get_job(operation.job_handle)
            if operation is not None and operation.job_handle is not None
            else None
        )
        first_processing = current is not None and current.state == JobState.RUNNING
        if evidence.terminal is NativeTurnTerminal.INTERRUPTED and (
            first_write or first_processing
        ):
            # Do this before finishing the active job: an exception at that recoverable boundary
            # must not make a later retry miss queue cancellation.
            self._cancel_pending_followups(participant_id, evidence)
        if operation is not None and operation.job_handle is not None:
            self._clear_execution_barrier_for_operation(operation)
            job = self._finish_from_evidence(participant_id, operation.job_handle, evidence)
        # Historical interruptions can leave unrelated newer followups. They
        # need the same automatic progress opportunity as normal completions.
        self.schedule_dispatch(participant_id)
        self._schedule_maintenance(participant_id)
        return job

    def _finish_from_evidence(
        self, participant_id: str, job_handle: str, evidence: NativeTerminalEvidence
    ) -> Job | None:
        """Finish one job from persisted terminal evidence; exactly once."""
        job = self._store.get_job(job_handle)
        if job is None:
            logger.warning(
                "terminal evidence for %s turn %s maps to missing job %s",
                participant_id,
                evidence.native_turn_id,
                job_handle,
            )
            return None
        if job.state != JobState.RUNNING:
            # Repeated or delayed evidence for an already-terminal job never
            # rewrites its terminal state; the first write stands.
            return job
        state = _JOB_STATE_FOR_TERMINAL[evidence.terminal]
        result = evidence.result
        if evidence.terminal is NativeTurnTerminal.INTERRUPTED:
            result = result or (
                "The native turn was interrupted; the job was cancelled with it. "
                "Re-send the prompt if the work is still wanted."
            )
        error_code = (
            evidence.error_code
            if evidence.error_code is not None
            else (
                INTERRUPTED_ERROR_CODE
                if evidence.terminal is NativeTurnTerminal.INTERRUPTED
                else None
            )
        )
        return self._jobs.finish(job_handle, state=state, result=result, error_code=error_code)

    # ---- restart and reconciliation ---------------------------------------

    def fail_undelivered_followups(
        self,
        participant_ids: list[str],
        *,
        error_code: str = DAEMON_RESTARTED_ERROR_CODE,
        preserve_legacy_queued: bool = False,
    ) -> list[Job]:
        """Settle restart residue without replaying possibly delivered work."""
        failed: list[Job] = []
        for participant_id in participant_ids:
            failed.extend(self._fail_reserved_operations(participant_id, error_code))
            self._settle_non_prompt_dispatched(participant_id)
            failed.extend(
                self._fail_queued_followups(
                    participant_id,
                    error_code,
                    preserve_legacy_queued=preserve_legacy_queued,
                )
            )
            failed.extend(self._reconcile_running_jobs_at_restart(participant_id, error_code))
        return failed

    def _fail_reserved_operations(self, participant_id: str, error_code: str) -> list[Job]:
        """Settle every RESERVED operation — job-bearing and jobless."""
        failed: list[Job] = []
        for operation in self._store.control_operations_in_phases(
            participant_id, (ControlDeliveryPhase.RESERVED,)
        ):
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.REJECTED,
                error_code=error_code,
                error=(
                    (
                        "the Theater daemon restarted before this "
                        f"{operation.kind.value} operation was transmitted; it "
                        "is definitively never delivered and never retried"
                    )
                    if operation.job_handle is None
                    else (
                        "the Theater daemon restarted before transmission "
                        "began; the delivery is never retried"
                    )
                ),
                updated_at=self._clock(),
            )
            if operation.kind not in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP):
                continue
            if operation.job_handle is not None:
                current = self._store.get_job(operation.job_handle)
                if current is not None and current.state == JobState.RUNNING:
                    failed_job = self._jobs.finish(
                        operation.job_handle,
                        state=JobState.CRASHED,
                        result=_UNDELIVERED_RESTART_RESULT,
                        error_code=error_code,
                    )
                    if failed_job is not None:
                        failed.append(failed_job)
        return [job for job in failed if job is not None]

    def _settle_non_prompt_dispatched(self, participant_id: str) -> None:
        """Settle every non-prompt DISPATCHED operation as ``unknown``."""
        for operation in self._store.control_operations_in_phases(
            participant_id, (ControlDeliveryPhase.DISPATCHED,)
        ):
            if operation.kind in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP):
                continue
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.UNKNOWN,
                error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                error=(
                    "the Theater daemon restarted after this "
                    f"{operation.kind.value} operation's transmission began; "
                    "its acknowledgement is unknown and it is never "
                    "retried"
                ),
                updated_at=self._clock(),
            )
            self._count_unknown_delivery(operation.kind, CONTROL_UNKNOWN_RESTART)

    def _fail_queued_followups(
        self,
        participant_id: str,
        error_code: str,
        *,
        preserve_legacy_queued: bool,
    ) -> list[Job]:
        """Fail queued followups unless their exact route remains recoverable."""
        failed: list[Job] = []
        for operation in self._store.queued_control_operations(participant_id):
            if operation.transport is ControlTransport.PROVIDER_TERMINAL or (
                preserve_legacy_queued and operation.transport is ControlTransport.LEGACY_TMUX
            ):
                continue
            self._store.settle_control_operation(
                operation.operation_id,
                result=DeliveryResult.REJECTED,
                error_code=error_code,
                error="the Theater daemon restarted before this followup was "
                "delivered; it is never replayed automatically",
                updated_at=self._clock(),
            )
            if operation.job_handle:
                job = self._jobs.finish(
                    operation.job_handle,
                    state=JobState.CRASHED,
                    result=(
                        "Queued followup failed: the Theater daemon restarted "
                        "before it was delivered, and undelivered followups are "
                        "never replayed. Queue the prompt again if the work is "
                        "still wanted."
                    ),
                    error_code=error_code,
                )
                if job is not None:
                    failed.append(job)
        return failed

    def _reconcile_running_jobs_at_restart(self, participant_id: str, error_code: str) -> list[Job]:
        """Close the two remaining crash windows around running jobs."""
        failed: list[Job] = []
        for job in self._store.running_jobs_for_target(participant_id):
            operations = self._store.control_operations_for_job(job.handle)
            if not operations:
                if self.route_for(
                    participant_id, RuntimeCapability.SEND
                ).is_native and job.kind in (
                    "send",
                    "spawn",
                ):
                    orphan = self._jobs.finish(
                        job.handle,
                        state=JobState.CRASHED,
                        result=_UNDELIVERED_RESTART_RESULT,
                        error_code=error_code,
                    )
                    if orphan is not None:
                        failed.append(orphan)
                continue  # a legacy job with no operation: the observer owns it
            for op in operations:
                if (
                    op.kind in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP)
                    and op.delivery_phase is ControlDeliveryPhase.SETTLED
                    and op.delivery_result is DeliveryResult.REJECTED
                ):
                    # Crash after the operation settled, before the job
                    # finish: close it from the stored refusal facts.
                    settled_job = self._jobs.finish(
                        job.handle,
                        state=JobState.CRASHED,
                        result=op.error or "the native backend refused the prompt",
                        error_code=op.error_code or SEND_REJECTED_ERROR_CODE,
                    )
                    if settled_job is not None:
                        failed.append(settled_job)
                    break
        return failed

    def finish_jobs_from_pending_evidence(self, participant_ids: list[str]) -> list[Job]:
        """Close the crash window between the evidence commit and job finish."""
        finished: list[Job] = []
        for participant_id in participant_ids:
            for job in self._store.active_running_jobs_for_target(participant_id):
                for operation in self._store.control_operations_for_job(job.handle):
                    if operation.transport is not ControlTransport.NATIVE_RUNTIME:
                        continue
                    if (
                        operation.backend_generation is None
                        or operation.native_session_id is None
                        or operation.native_turn_id is None
                    ):
                        continue
                    evidence = self._store.get_native_terminal_evidence(
                        participant_id=participant_id,
                        backend_generation=operation.backend_generation,
                        native_session_id=operation.native_session_id,
                        native_turn_id=operation.native_turn_id,
                    )
                    if evidence is None:
                        continue
                    if evidence.terminal is NativeTurnTerminal.INTERRUPTED:
                        self._cancel_pending_followups(participant_id, evidence)
                    self._clear_execution_barrier_for_operation(operation)
                    result = self._finish_from_evidence(participant_id, job.handle, evidence)
                    if result is not None and result.state != JobState.RUNNING:
                        finished.append(result)
                        break
        return finished

    async def reconcile_ambiguous_delivery(
        self, participant_id: str, *, now_ts: float
    ) -> list[Job]:
        """Reconcile uncertain prompt execution without ever mutating it."""
        runtime = self._runtime_for(participant_id)
        resolved: list[Job] = []
        async with self._lock(participant_id):
            operations = {
                operation.operation_id: operation
                for operation in (
                    *self._store.execution_barrier_control_operations(participant_id),
                    *self._store.unresolved_prompt_delivery_operations(participant_id),
                )
                if operation.transport is ControlTransport.NATIVE_RUNTIME
            }
            # The commit-before-finish crash window is resolved before the deadline path.
            if operations:
                resolved.extend(self.finish_jobs_from_pending_evidence([participant_id]))
                operations = {
                    operation.operation_id: operation
                    for operation in (
                        *self._store.execution_barrier_control_operations(participant_id),
                        *self._store.unresolved_prompt_delivery_operations(participant_id),
                    )
                    if operation.transport is ControlTransport.NATIVE_RUNTIME
                }
            snapshot: RuntimeSnapshot | None = None
            if runtime is not None and operations:
                try:
                    snapshot = await runtime.snapshot()
                    self._gates.record_native_snapshot(participant_id, runtime, snapshot)
                except Exception as exc:
                    # A failed state read is UNKNOWN, never idle.
                    logger.warning(
                        "could not snapshot %s during control reconciliation: %s",
                        participant_id,
                        exc,
                    )
            for operation in operations.values():
                resolved.extend(
                    self._reconcile_one_delivery(
                        participant_id, operation, snapshot=snapshot, now_ts=now_ts
                    )
                )
        return resolved

    def _reconcile_one_delivery(
        self,
        participant_id: str,
        operation: ControlOperation,
        *,
        snapshot: RuntimeSnapshot | None,
        now_ts: float,
    ) -> list[Job]:
        """Resolve one ambiguous operation from exact native facts only."""
        evidence = None
        if (
            operation.backend_generation is not None
            and operation.native_session_id is not None
            and operation.native_turn_id is not None
        ):
            evidence = self._store.get_native_terminal_evidence(
                participant_id=participant_id,
                backend_generation=operation.backend_generation,
                native_session_id=operation.native_session_id,
                native_turn_id=operation.native_turn_id,
            )
        if evidence is not None:
            # Committed terminal evidence outranks the snapshot: the turn is over, whatever a stale
            # snapshot still reports.
            self._clear_execution_barrier_for_operation(operation)
            if operation.job_handle is None:
                return []  # a jobless operation carries no recovery obligation
            result = self._finish_from_evidence(participant_id, operation.job_handle, evidence)
            return [] if result is None else [result]
        same_backend_session = (
            snapshot is not None
            and operation.backend_generation is not None
            and operation.native_session_id is not None
            and snapshot.backend_generation == operation.backend_generation
            and snapshot.native_session_id == operation.native_session_id
        )
        barrier_released = False
        if (
            same_backend_session
            and snapshot is not None
            and self._is_authoritatively_idle(snapshot)
        ):
            # This proves the exact execution boundary is clear, not that the prompt completed
            # successfully.
            self._clear_execution_barrier_for_operation(operation, preserve_deadline=True)
            barrier_released = operation.execution_barrier
        if (
            same_backend_session
            and snapshot is not None
            and snapshot.execution_state is RuntimeExecutionState.ACTIVE
            and operation.native_turn_id is not None
            and snapshot.native_turn_id == operation.native_turn_id
        ):
            return []  # exact known active turn: keep waiting for evidence
        # Startup must drain buffered exact evidence before enforcing old delivery deadlines.
        if self._recovering:
            return []
        deadline = max(
            operation.updated_at + AMBIGUOUS_DELIVERY_DEADLINE_SECONDS,
            self._deadline_not_before.get(operation.operation_id, -math.inf),
        )
        if now_ts < deadline:
            return []  # still inside the immediate reconciliation window
        job = self._store.get_job(operation.job_handle) if operation.job_handle else None
        if job is None or job.state != JobState.RUNNING:
            # A terminal job is immutable.  If its barrier was already
            # released by exact idle, its deadline floor is no longer needed.
            if barrier_released or not operation.execution_barrier:
                self._deadline_not_before.pop(operation.operation_id, None)
            return []
        if operation.job_handle is None:
            return []  # a jobless operation carries no recovery obligation
        finished = self._jobs.finish(
            operation.job_handle,
            state=JobState.CRASHED,
            result=(
                "Delivery of this prompt could not be confirmed within "
                f"{AMBIGUOUS_DELIVERY_DEADLINE_SECONDS:.0f}s and the "
                "backend never produced terminal evidence for it. "
                "WARNING: native work may have been accepted and may "
                "still be running; Theater did not resend or fall back "
                "to the pane. Inspect the participant, then re-send if "
                "the work is still wanted."
            ),
            error_code=DELIVERY_UNKNOWN_ERROR_CODE,
        )
        if finished is not None:
            # The deadline closed a job the backend never resolved; the explicit warning above stays
            # the human-facing record.
            self._deadline_not_before.pop(operation.operation_id, None)
            self._count_unknown_delivery(operation.kind, CONTROL_UNKNOWN_DEADLINE)
            return [finished]
        return []

    # ---- active-job selectors for observation integration ------------------

    def active_jobs(self, participant_id: str) -> list[Job]:
        """Running jobs actually delivered to the participant, oldest first."""
        return self._store.active_running_jobs_for_target(participant_id)

    def project_action(
        self,
        participant_id: str,
        capability: RuntimeCapability,
        *,
        route: ControlRoute,
        route_available: bool,
        alive: bool,
        presence: str,
        presence_detail: str | None = None,
        connection=None,
    ) -> dict[str, object]:
        """Project cached action availability without entering the mutation service."""
        return self._projection.project_action(
            participant_id,
            capability,
            route=route,
            route_available=route_available,
            alive=alive,
            presence=presence,
            presence_detail=presence_detail,
            connection=connection,
        )

    def active_job_for_native_turn(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
        connection=None,
    ) -> Job | None:
        """The exact running job bound to one native turn, or ``None``."""
        operation = self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            connection=connection,
        )
        if operation is None or operation.job_handle is None:
            return None
        job = self._store.get_job(operation.job_handle, connection=connection)
        if job is None or job.state != JobState.RUNNING:
            return None
        return job

    def queued_jobs(self, participant_id: str) -> list[Job]:
        """Pending followup jobs in FIFO order — for exclusion, never completion."""
        jobs: list[Job] = []
        for operation in self._store.queued_control_operations(participant_id):
            if operation.job_handle:
                job = self._store.get_job(operation.job_handle)
                if job is not None and job.state == JobState.RUNNING:
                    jobs.append(job)
        return jobs

    # ---- internals ---------------------------------------------------------

    def _queue_predecessor(self, participant_id: str, snapshot: RuntimeSnapshot) -> str | None:
        """Capture exact execution context, never a stale completed job."""
        turn = snapshot.native_turn_id
        session = snapshot.native_session_id
        if (
            turn is None
            or session is None
            or snapshot.health not in (ConnectionHealth.CONNECTED, ConnectionHealth.DEGRADED)
        ):
            return None
        if (
            self._store.get_native_terminal_evidence(
                participant_id=participant_id,
                backend_generation=snapshot.backend_generation,
                native_session_id=session,
                native_turn_id=turn,
            )
            is not None
        ):
            return None
        operation = self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=snapshot.backend_generation,
            native_session_id=session,
            native_turn_id=turn,
        )
        if operation is not None and operation.job_handle is not None:
            job = self._store.get_job(operation.job_handle)
            if job is None or job.state != JobState.RUNNING:
                return None
        return turn

    @staticmethod
    def _queue_payload(*, predecessor: str | None, callback_operation_id: str | None) -> str | None:
        values = {
            key: value
            for key, value in (
                ("queue_predecessor_turn", predecessor),
                ("callback_operation_id", callback_operation_id),
            )
            if value is not None
        }
        return json.dumps(values) if values else None

    @staticmethod
    def _callback_operation_id(operation: ControlOperation) -> str | None:
        if operation.payload is None:
            return None
        try:
            value = json.loads(operation.payload)
        except (TypeError, ValueError):
            return None
        callback_id = value.get("callback_operation_id") if isinstance(value, dict) else None
        return callback_id if isinstance(callback_id, str) and callback_id else None

    def _bind_queued_predecessor(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        turn: str,
        *,
        connection=None,
    ) -> None:
        """Move the bounded pending FIFO behind an actually observed/accepted turn."""
        for operation in self._store.queued_control_operations(
            participant_id, connection=connection
        ):
            payload = self._queue_payload(
                predecessor=turn,
                callback_operation_id=self._callback_operation_id(operation),
            )
            assert payload is not None
            if (
                operation.transport is ControlTransport.NATIVE_RUNTIME
                and operation.backend_generation == snapshot.backend_generation
                and operation.native_session_id == snapshot.native_session_id
                and operation.payload != payload
            ):
                self._store.set_queued_control_payload(
                    operation.operation_id, payload, connection=connection
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

    def _record_legacy_send(
        self,
        participant_id: str,
        *,
        caller_id: str,
        job: Job,
        prompt: str,
    ) -> None:
        self._store.bus_append(
            "agent.send",
            from_id=caller_id,
            to_id=participant_id,
            payload={"handle": job.handle, "prompt": prompt[:200]},
        )

    def _disconnected_native_refusal(self, participant_id: str, control: str) -> StaleTarget:
        """A persisted native binding whose runtime is gone: fail closed."""
        return StaleTarget(
            f"participant {participant_id!r} is natively wired but its runtime is "
            f"not connected (detached or recovering); the {control} is refused and "
            "never falls back to the legacy pane, is never queued as legacy work, "
            "and is never retried automatically — wait for the runtime to "
            "reconnect (reconcile/adopt) or restart the participant, then issue "
            "the control again"
        )

    def _create_send_job(
        self,
        participant_id: str,
        *,
        caller_id: str,
        prompt: str,
        response_format: str | None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
    ) -> Job:
        handle = f"{participant_id}#{self._mint_sequence()}"
        self._jobs.create(
            handle=handle,
            caller_id=caller_id,
            target_id=participant_id,
            kind="send",
            prompt=prompt,
            cwd=self._gates.cwd_for(participant_id),
            response_format=response_format,
            actor_client_id=actor_client_id,
            actor_participant_id=actor_participant_id,
        )
        return self._require_job(handle)

    def _reusable_spawn_job(
        self,
        participant_id: str,
        *,
        job_handle: str,
        caller_id: str,
        prompt: str,
        response_format: str | None,
    ) -> Job:
        """Validate a job handle for native initial-dispatch reuse."""
        job = self._store.get_job(job_handle)
        if job is None:
            raise BadRequest(
                f"job {job_handle!r} does not exist; the initial dispatch of "
                f"participant {participant_id!r} cannot reuse it"
            )
        if job.kind != "spawn":
            raise BadRequest(
                f"job {job_handle!r} is a {job.kind!r} job, not the spawn job of "
                f"participant {participant_id!r}; initial-dispatch reuse accepts "
                "exactly the spawn job and creates no second job"
            )
        if job.state != JobState.RUNNING:
            raise BadRequest(
                f"job {job_handle!r} is already {job.state}; the initial "
                f"dispatch of participant {participant_id!r} can only reuse a "
                "running spawn job"
            )
        if job.target_id != participant_id:
            raise BadRequest(
                f"job {job_handle!r} belongs to target {job.target_id!r}, not "
                f"{participant_id!r}; refusing to dispatch another participant's "
                "spawn job"
            )
        if job.caller_id != caller_id:
            raise BadRequest(
                f"job {job_handle!r} was created by caller {job.caller_id!r}, not "
                f"{caller_id!r}; the initial dispatch must keep the spawn's caller "
                "contract"
            )
        if (job.prompt or "") != prompt:
            raise BadRequest(
                f"job {job_handle!r} carries a different prompt than the one being "
                f"dispatched to participant {participant_id!r}; the initial "
                "dispatch must be exactly the spawn's prompt"
            )
        if job.response_format != response_format:
            raise BadRequest(
                f"job {job_handle!r} carries response_format "
                f"{job.response_format!r}, not {response_format!r}; the initial "
                "dispatch must keep the spawn's response-format contract"
            )
        if self._store.control_operations_for_job(job_handle):
            # The initial dispatch happens exactly once.
            raise BadRequest(
                f"job {job_handle!r} already carries a control operation; the "
                f"initial dispatch of participant {participant_id!r} happens "
                "exactly once and is never retransmitted"
            )
        return job

    def _require_job(self, handle: str) -> Job:
        job = self._store.get_job(handle)
        assert job is not None
        return job

    def reject_public_reservation(self, operation_id: str, exc: Exception) -> None:
        """Close only a public control proven not to have begun dispatch."""
        operation = self._store.get_control_operation(operation_id)
        if operation is None or operation.delivery_phase not in {
            ControlDeliveryPhase.RESERVED,
            ControlDeliveryPhase.QUEUED,
        }:
            return
        error_code = _error_code_of(exc)
        self._store.settle_control_operation(
            operation_id,
            result=DeliveryResult.REJECTED,
            error_code=error_code,
            error=str(exc),
            updated_at=self._clock(),
        )
        self._control_notifier.notify(operation_id)
        if operation.job_handle is not None and operation.kind in {
            ControlKind.SEND,
            ControlKind.QUEUE_FOLLOWUP,
        }:
            self._jobs.finish(
                operation.job_handle,
                state=JobState.CRASHED,
                result=str(exc),
                error_code=error_code,
            )

    def _require_public_reservation(
        self,
        operation_id: str | None,
        *,
        participant_id: str,
        kind: ControlKind,
        phase: ControlDeliveryPhase,
        route: ControlRoute,
        job_handle: str | None = None,
    ) -> ControlOperation:
        if operation_id is None:
            raise RuntimeError("a pre-reserved public control requires its durable ID")
        operation = self._store.get_control_operation(operation_id)
        if operation is None:
            raise RuntimeError(f"public control reservation {operation_id!r} disappeared")
        if (
            operation.participant_id != participant_id
            or operation.kind is not kind
            or operation.delivery_phase is not phase
            or operation.transport is not route.transport
            or (job_handle is not None and operation.job_handle != job_handle)
        ):
            raise StaleTarget(
                f"public control reservation {operation_id!r} no longer matches its target"
            )
        if route.is_provider:
            terminal = self._provider.require(participant_id, route.capability, route)
            expected = (
                operation.provider_id,
                operation.provider_generation,
                operation.terminal_id,
                operation.terminal_incarnation,
            )
            current = (
                terminal.provider_id,
                terminal.provider_generation,
                terminal.terminal_id,
                terminal.terminal_incarnation,
            )
            if expected != current:
                raise StaleTarget(
                    f"provider terminal identity for participant {participant_id!r} changed "
                    "after public control admission"
                )
        return operation

    @staticmethod
    def _require_reserved_native_identity(
        operation: ControlOperation, snapshot: RuntimeSnapshot
    ) -> None:
        if operation.backend_generation is None or operation.native_session_id is None:
            raise StaleTarget("native control reservation has no exact session identity")
        if operation.backend_generation != snapshot.backend_generation:
            raise StaleTarget("native backend generation changed after public control admission")
        if operation.native_session_id != snapshot.native_session_id:
            raise StaleTarget("native session changed after public control admission")

    @staticmethod
    def _notify_reserved(
        callback: Callable[[str, str | None], None] | None,
        operation_id: str,
        job_handle: str | None,
    ) -> None:
        if callback is not None:
            callback(operation_id, job_handle)

    def _reserve(
        self,
        operation_id: str,
        *,
        participant_id: str,
        kind: ControlKind,
        transport: ControlTransport,
        phase: ControlDeliveryPhase,
        job_handle: str | None = None,
        backend_generation: int | None = None,
        native_session_id: str | None = None,
        native_turn_id: str | None = None,
        provider_id: str | None = None,
        provider_generation: int | None = None,
        terminal_id: str | None = None,
        terminal_incarnation: str | None = None,
        queue_sequence: int | None = None,
        payload: str | None = None,
        connection=None,
    ) -> None:
        """Persist the operation before transmission; the id is its identity."""
        timestamp = self._clock()
        self._store.reserve_control_operation(
            _operation_row(
                operation_id=operation_id,
                participant_id=participant_id,
                kind=kind,
                transport=transport,
                phase=phase,
                job_handle=job_handle,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                native_turn_id=native_turn_id,
                provider_id=provider_id,
                provider_generation=provider_generation,
                terminal_id=terminal_id,
                terminal_incarnation=terminal_incarnation,
                queue_sequence=queue_sequence,
                payload=payload,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=connection,
        )

    async def _deliver_native(
        self,
        runtime: HarnessRuntime,
        *,
        kind: ControlKind,
        participant_id: str,
        operation_id: str,
        prompt: str,
        job_handle: str,
        snapshot: RuntimeSnapshot,
    ) -> DeliveryResult | None:
        """DISPATCHED before transmission; settle from the receipt; no retry."""
        operation = self._store.get_control_operation(operation_id)
        if operation is None:
            raise RuntimeError(f"native control reservation {operation_id!r} disappeared")
        self._require_reserved_native_identity(operation, snapshot)
        self._store.mark_control_operation_dispatched(
            operation_id,
            native_session_id=snapshot.native_session_id,
            execution_barrier=True,
            updated_at=self._clock(),
        )
        # Arm durable reconciliation before the runtime write.
        self._schedule_maintenance(participant_id)
        try:
            receipt = await runtime.send(operation_id=operation_id, prompt=prompt)
        except asyncio.CancelledError:
            # ``turn/start`` may have crossed the transport write before the caller's cancellation
            # arrived.
            self._settle_uncertain(
                operation_id,
                execution_barrier=True,
                error=(
                    "the prompt delivery was cancelled after transmission began; "
                    "its acknowledgement is unknown and it is never retried"
                ),
            )
            self._count_unknown_delivery(kind, CONTROL_UNKNOWN_ACK_LOST)
            self._schedule_maintenance(participant_id)
            raise
        except Exception as exc:
            logger.warning(
                "delivery of %s to %s is uncertain (acknowledgement lost): %s; "
                "no retry, no tmux fallback",
                operation_id,
                participant_id,
                exc,
            )
            self._count_unknown_delivery(kind, CONTROL_UNKNOWN_ACK_LOST)
            self._schedule_maintenance(participant_id)
            return None
        if not self._receipt_names_operation(operation_id, receipt):
            # Mismatched receipts leave delivery UNKNOWN until bounded reconciliation resolves it.
            self._settle_uncertain(
                operation_id,
                execution_barrier=True,
                error=(
                    f"the native receipt named operation {receipt.operation_id!r}, "
                    f"not {operation_id!r}; the delivery is uncertain and the "
                    "receipt is not trusted to settle it"
                ),
            )
            self._count_unknown_delivery(kind, CONTROL_UNKNOWN_RECEIPT_MISMATCH)
            self._schedule_maintenance(participant_id)
            return DeliveryResult.UNKNOWN
        if receipt.result is DeliveryResult.REJECTED:
            self._settle_from_receipt(operation_id, receipt, execution_barrier=False)
            self._jobs.finish(
                job_handle,
                state=JobState.CRASHED,
                result=receipt.error or "the native backend refused the prompt",
                error_code=receipt.error_code or SEND_REJECTED_ERROR_CODE,
            )
            return DeliveryResult.REJECTED
        if receipt.result is DeliveryResult.ACCEPTED:
            if receipt.native_turn_id is None:
                # Accepted but uncorrelated: without a native turn id the job can never be finished
                # by evidence.
                self._settle_uncertain(
                    operation_id,
                    execution_barrier=True,
                    error=(
                        "the native backend accepted the prompt but reported no "
                        "native turn id; the accepted turn cannot be correlated, "
                        "so the delivery stays uncertain"
                    ),
                )
                self._count_unknown_delivery(kind, CONTROL_UNKNOWN_UNCORRELATED)
                self._schedule_maintenance(participant_id)
                return DeliveryResult.UNKNOWN
            # Check before binding: two jobs sharing one exact native turn make completion
            # ambiguous.
            if self._turn_is_bound_to_another_job(participant_id, snapshot, receipt, job_handle):
                self._store.settle_control_operation(
                    operation_id,
                    result=DeliveryResult.REJECTED,
                    error_code=NATIVE_TURN_CONFLICT_ERROR_CODE,
                    error=(
                        f"native turn {receipt.native_turn_id!r} is already "
                        "bound to another Theater job"
                    ),
                    execution_barrier=False,
                    updated_at=self._clock(),
                )
                self._jobs.finish(
                    job_handle,
                    state=JobState.CRASHED,
                    result=(
                        f"the native backend reported turn {receipt.native_turn_id!r}, "
                        "which is already bound to another Theater job; refusing "
                        "to bind two jobs to one native turn"
                    ),
                    error_code=NATIVE_TURN_CONFLICT_ERROR_CODE,
                )
                return DeliveryResult.REJECTED
            with self._store.write_unit() as unit:
                connection = unit.connection
                self._settle_from_receipt(
                    operation_id, receipt, execution_barrier=False, connection=connection
                )
                self._bind_queued_predecessor(
                    participant_id, snapshot, receipt.native_turn_id, connection=connection
                )
                settled = self._store.get_control_operation(operation_id, connection=connection)
                assert settled is not None
                event = control_event(
                    self._store,
                    settled,
                    connection,
                    revision=next_revision(self._store, connection),
                )
                if event is not None:
                    self._store.journal.append_group(unit, [event])
            return DeliveryResult.ACCEPTED
        # An uncertain delivery settles UNKNOWN with the turn it named, if any: never retried, never
        # tmux-fallback, eligible only for exact evidence or snapshot reconciliation.
        self._settle_from_receipt(operation_id, receipt, execution_barrier=True)
        if receipt.result is DeliveryResult.UNKNOWN and snapshot.native_session_id is not None:
            logger.warning(
                "delivery of %s to %s stayed uncertain (turn %s); no retry, "
                "no tmux fallback — evidence or the snapshot is the only path",
                operation_id,
                participant_id,
                receipt.native_turn_id,
            )
        self._count_unknown_delivery(kind, CONTROL_UNKNOWN_RECEIPT_UNKNOWN)
        self._schedule_maintenance(participant_id)
        return receipt.result

    def _receipt_names_operation(self, operation_id: str, receipt: ControlReceipt) -> bool:
        """A receipt is authoritative only for the operation it names."""
        if receipt.operation_id == operation_id:
            return True
        logger.error(
            "native receipt named operation %r, expected %r; the receipt is not "
            "trusted to settle the reserved operation",
            receipt.operation_id,
            operation_id,
        )
        return False

    def _settle_uncertain(
        self,
        operation_id: str,
        *,
        error: str,
        execution_barrier: bool | None = None,
    ) -> None:
        """Settle one operation as uncertain: never retried, never fallback."""
        self._store.settle_control_operation(
            operation_id,
            result=DeliveryResult.UNKNOWN,
            error_code=DELIVERY_UNKNOWN_ERROR_CODE,
            error=error,
            execution_barrier=execution_barrier,
            updated_at=self._clock(),
        )

    def _settle_from_receipt(
        self,
        operation_id: str,
        receipt: ControlReceipt,
        *,
        execution_barrier: bool | None = None,
        connection=None,
    ) -> None:
        self._store.settle_control_operation(
            operation_id,
            result=receipt.result,
            native_turn_id=receipt.native_turn_id,
            error_code=receipt.error_code,
            error=receipt.error,
            execution_barrier=execution_barrier,
            updated_at=self._clock(),
            connection=connection,
        )

    def _turn_is_bound_to_another_job(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        receipt: ControlReceipt,
        job_handle: str,
    ) -> bool:
        """Never bind two Theater jobs to one native turn; fail closed."""
        turn = receipt.native_turn_id
        if turn is None or snapshot.native_session_id is None:
            return False
        try:
            operation = self._store.control_operation_for_native_turn(
                participant_id=participant_id,
                backend_generation=snapshot.backend_generation,
                native_session_id=snapshot.native_session_id,
                native_turn_id=turn,
            )
        except ControlOperationAmbiguityError:
            logger.error(  # noqa: TRY400 - a controlled fail-closed, not a crash
                "native turn %s of %s already maps to multiple job-bearing "
                "operations; failing job %s closed instead of settling into "
                "an ambiguous mapping",
                turn,
                participant_id,
                job_handle,
            )
            return True
        if operation is None or operation.job_handle == job_handle:
            return False
        logger.error(
            "native turn %s of %s is already bound to job %s; failing job %s "
            "closed instead of binding two jobs to one turn",
            turn,
            participant_id,
            operation.job_handle,
            job_handle,
        )
        return True

    def _require_capability(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        capability: RuntimeCapability,
        action: str,
    ) -> None:
        """Fails-closed capability gate at execution; no fallback ever."""
        if snapshot.capabilities.supports(capability):
            return
        reason = snapshot.capabilities.reason_for(capability)
        raise BadRequest(
            f"participant {participant_id!r} does not support {action} "
            f"({reason}); the native runtime gates this capability, so the "
            "control is refused — never retried, never fallen back"
        )

    @staticmethod
    def _is_authoritatively_idle(snapshot: RuntimeSnapshot) -> bool:
        """Whether native facts prove it is safe to start a prompt."""
        return (
            snapshot.execution_state is RuntimeExecutionState.IDLE
            and snapshot.health in (ConnectionHealth.CONNECTED, ConnectionHealth.DEGRADED)
            and snapshot.native_session_id is not None
            and snapshot.native_turn_id is None
            and snapshot.pending_interaction is None
        )

    def _clear_execution_barriers_from_idle_snapshot(
        self, participant_id: str, snapshot: RuntimeSnapshot
    ) -> None:
        """Clear only barriers proven idle on their exact backend/session."""
        if not self._is_authoritatively_idle(snapshot):
            return
        for operation in self._store.execution_barrier_control_operations(participant_id):
            if (
                operation.transport is ControlTransport.NATIVE_RUNTIME
                and operation.backend_generation == snapshot.backend_generation
                and operation.native_session_id == snapshot.native_session_id
            ):
                self._clear_execution_barrier_for_operation(operation, preserve_deadline=True)

    def _clear_execution_barrier_for_operation(
        self, operation: ControlOperation, *, preserve_deadline: bool = False
    ) -> None:
        """Release a barrier from exact evidence or exact authoritative idle."""
        if not operation.execution_barrier:
            return
        self._store.set_control_execution_barrier(
            operation.operation_id,
            active=False,
            updated_at=operation.updated_at if preserve_deadline else self._clock(),
        )
        if not preserve_deadline:
            self._deadline_not_before.pop(operation.operation_id, None)

    def _reject_busy(
        self,
        participant_id: str,
        snapshot: RuntimeSnapshot,
        *,
        operation: BusyOperation,
        exclude: str | None = None,
    ) -> None:
        """Authoritative idle/busy check from the runtime snapshot and store."""
        if snapshot.pending_interaction is not None:
            raise AwaitingDecision(
                f"participant {participant_id!r} is waiting for a human to answer "
                f"a native {snapshot.pending_interaction.kind.value}; only the "
                "native UI may answer it — not Theater, not the caller"
            )
        refusal = self._busy_action(participant_id, snapshot, exclude=exclude)
        if refusal is None:
            return
        raise busy_refusal(
            participant_id,
            refusal,
            turn=snapshot.native_turn_id,
            operation=operation,
        )

    def _busy_action(
        self, participant_id: str, snapshot: RuntimeSnapshot, *, exclude: str | None
    ) -> BusyRefusal | None:
        """First applicable refusal, walking :class:`BusyAction`'s declared order.

        The loop is the ordering contract: reordering the enum reorders
        selection, and a new member must earn its place in the walk.
        """
        facts = self._busy_facts(participant_id, snapshot, exclude=exclude)
        for action in BusyAction:
            refusal = self._refusal_for(action, facts)
            if refusal is not None:
                return refusal
        return None

    def _busy_facts(
        self, participant_id: str, snapshot: RuntimeSnapshot, *, exclude: str | None
    ) -> BusyFacts:
        """Gather the walk's facts; barrier and job facts only on the idle path."""
        queued = self._store.queued_control_operation_count(participant_id)
        idle = self._is_authoritatively_idle(snapshot)
        barrier = False
        running_handle: str | None = None
        if idle and not queued:
            self._clear_execution_barriers_from_idle_snapshot(participant_id, snapshot)
            barrier = self._store.has_execution_barrier(participant_id)
            if not barrier:
                active = self._store.active_running_jobs_for_target(participant_id)
                if exclude is not None:
                    active = [job for job in active if job.handle != exclude]
                if active:
                    running_handle = active[0].handle
        return BusyFacts(
            idle=idle,
            connected=snapshot.health
            not in (ConnectionHealth.DISCONNECTED, ConnectionHealth.UNOPENED),
            identified=snapshot.native_session_id is not None,
            active=snapshot.execution_state is RuntimeExecutionState.ACTIVE,
            queued=queued,
            barrier=barrier,
            running_handle=running_handle,
        )

    def _refusal_for(self, action: BusyAction, facts: BusyFacts) -> BusyRefusal | None:
        """One action's applicability; the walk supplies the order."""
        applicable = {
            BusyAction.RESTORE_RUNTIME: not facts.idle and not facts.connected,
            BusyAction.RESTORE_IDENTITY: not facts.idle and not facts.identified,
            BusyAction.RESOLVE_UNKNOWN_STATE: not facts.idle and not facts.active,
            BusyAction.AWAIT_QUEUE: facts.queued > 0,
            BusyAction.AWAIT_TURN_END: not facts.idle,
            BusyAction.AWAIT_BARRIER: facts.idle and facts.barrier,
            BusyAction.AWAIT_JOBS: facts.running_handle is not None,
        }
        if not applicable[action]:
            return None
        return BusyRefusal(action, queued=facts.queued, running_handle=facts.running_handle)

    def _operation_for_snapshot_turn(self, participant_id: str, snapshot: RuntimeSnapshot):
        if snapshot.native_session_id is None or snapshot.native_turn_id is None:
            return None
        return self._operation_for_turn(
            participant_id=participant_id,
            backend_generation=snapshot.backend_generation,
            native_session_id=snapshot.native_session_id,
            native_turn_id=snapshot.native_turn_id,
        )

    def _operation_for_turn(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
        connection=None,
    ):
        """The exact-turn lookup — the only job-to-turn mapping there is."""
        try:
            return self._store.control_operation_for_native_turn(
                participant_id=participant_id,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                native_turn_id=native_turn_id,
                connection=connection,
            )
        except ControlOperationAmbiguityError as exc:
            # A duplicate job-bearing mapping is a bug state; failing closed
            # means completing nothing, never guessing.
            logger.error(  # noqa: TRY400 - a controlled fail-closed, not a crash
                "native turn %s of %s maps to multiple job-bearing operations; failing closed: %s",
                native_turn_id,
                participant_id,
                exc,
            )
            return None


def _error_code_of(exc: Exception) -> str:
    return getattr(exc, "code", None) or "dispatch_failed"


def _operation_row(
    *,
    operation_id: str,
    participant_id: str,
    kind: ControlKind,
    transport: ControlTransport,
    phase: ControlDeliveryPhase,
    created_at: float,
    updated_at: float,
    job_handle: str | None = None,
    backend_generation: int | None = None,
    native_session_id: str | None = None,
    native_turn_id: str | None = None,
    provider_id: str | None = None,
    provider_generation: int | None = None,
    terminal_id: str | None = None,
    terminal_incarnation: str | None = None,
    queue_sequence: int | None = None,
    payload: str | None = None,
) -> ControlOperation:
    return ControlOperation(
        operation_id=operation_id,
        participant_id=participant_id,
        kind=kind,
        transport=transport,
        delivery_phase=phase,
        job_handle=job_handle,
        backend_generation=backend_generation,
        native_session_id=native_session_id,
        native_turn_id=native_turn_id,
        provider_id=provider_id,
        provider_generation=provider_generation,
        terminal_id=terminal_id,
        terminal_incarnation=terminal_incarnation,
        queue_sequence=queue_sequence,
        payload=payload,
        created_at=created_at,
        updated_at=updated_at,
    )
