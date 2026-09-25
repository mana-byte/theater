"""Typing-only contract of the state and cross-mixin methods ``ControlService`` supplies.

Each control mixin subclasses ``ControlHost``; at runtime it is plain ``object``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable

    from theater.daemon.controls.busy import BusyOperation
    from theater.daemon.controls.dispatch import QueueDispatchOutcome
    from theater.daemon.controls.gates import ControlGates
    from theater.daemon.controls.projection import ControlActionProjector
    from theater.daemon.controls.provider_delivery import ProviderControlDelivery
    from theater.daemon.controls.public_admission import PublicControlAdmission
    from theater.daemon.controls.routing import ControlRoute, ControlRouteResolver
    from theater.daemon.controls.service import _ControlLatency
    from theater.daemon.jobs import JobManager
    from theater.daemon.operations.notifications import OperationNotifier
    from theater.daemon.persistence.repositories.control_operations import ControlOperation
    from theater.daemon.persistence.repositories.native_evidence import NativeTerminalEvidence
    from theater.daemon.persistence.store import Store
    from theater.harness.contracts.runtime import (
        ControlDeliveryPhase,
        ControlKind,
        ControlReceipt,
        ControlTransport,
        DeliveryResult,
        HarnessRuntime,
        RuntimeCapability,
        RuntimeSnapshot,
    )
    from theater.models import Job, StaleTarget

    class ControlHost(Protocol):
        """The ``ControlService`` surface one mixin may use from another."""

        _runtime_for: Callable[[str], HarnessRuntime | None]
        _store: Store
        _jobs: JobManager
        _gates: ControlGates
        _projection: ControlActionProjector
        _native_identity_fencing: bool
        _routes: ControlRouteResolver
        _control_notifier: OperationNotifier
        _provider: ProviderControlDelivery
        _public_admission: PublicControlAdmission
        _locks: dict[str, asyncio.Lock]
        _dispatch_tasks: dict[str, asyncio.Task[QueueDispatchOutcome]]
        _maintenance_tasks: dict[str, asyncio.Task[None]]
        _maintenance_wakeups: dict[str, asyncio.Event]
        _maintenance_versions: dict[str, int]
        _deadline_not_before: dict[str, float]
        _scheduler_started: bool
        _recovering: bool
        _closing: bool

        def _count_unknown_delivery(self, kind: ControlKind, reason: str) -> None: ...
        def _control_latency(self, kind: ControlKind, participant_id: str) -> _ControlLatency: ...
        def route_for(
            self, participant_id: str, capability: RuntimeCapability, *, connection=None
        ) -> ControlRoute: ...
        def terminal_route_for(self, participant_id: str, *, connection=None) -> ControlRoute: ...
        async def _require_absent(self, participant_id: str, route: ControlRoute) -> None: ...
        def notify_persisted_settlement(self, operation_id: str) -> None: ...
        async def _snapshot_for_control(
            self, runtime: HarnessRuntime, participant_id: str, *, initial_dispatch: bool = False
        ) -> RuntimeSnapshot: ...
        def _require_current_native_route(
            self,
            participant_id: str,
            capability: RuntimeCapability,
            snapshot: RuntimeSnapshot,
            *,
            require_available: bool = True,
        ) -> ControlRoute: ...
        def schedule_dispatch(self, participant_id: str) -> None: ...
        def _schedule_maintenance(self, participant_id: str) -> None: ...
        def _has_maintenance_work(self, participant_id: str) -> bool: ...
        async def dispatch_queue(self, participant_id: str) -> QueueDispatchOutcome: ...
        def _cancel_pending_followups(
            self, participant_id: str, evidence: NativeTerminalEvidence | None = None
        ) -> tuple[str, ...]: ...
        def _finish_from_evidence(
            self, participant_id: str, job_handle: str, evidence: NativeTerminalEvidence
        ) -> Job | None: ...
        def finish_jobs_from_pending_evidence(self, participant_ids: list[str]) -> list[Job]: ...
        async def reconcile_ambiguous_delivery(
            self, participant_id: str, *, now_ts: float
        ) -> list[Job]: ...
        def active_job_for_native_turn(
            self,
            participant_id: str,
            *,
            backend_generation: int,
            native_session_id: str,
            native_turn_id: str,
            connection=None,
        ) -> Job | None: ...
        def _queue_predecessor(
            self, participant_id: str, snapshot: RuntimeSnapshot
        ) -> str | None: ...
        @staticmethod
        def _callback_operation_id(operation: ControlOperation) -> str | None: ...
        def _bind_queued_predecessor(
            self, participant_id: str, snapshot: RuntimeSnapshot, turn: str, *, connection=None
        ) -> None: ...
        def _lock(self, participant_id: str) -> asyncio.Lock: ...
        def _clock(self) -> float: ...
        def _mint_sequence(self, *, connection=None) -> int: ...
        def _mint_operation_id(self, participant_id: str, kind: ControlKind) -> str: ...
        def _record_legacy_send(
            self, participant_id: str, *, caller_id: str, job: Job, prompt: str
        ) -> None: ...
        def _disconnected_native_refusal(
            self, participant_id: str, control: str
        ) -> StaleTarget: ...
        def _require_job(self, handle: str) -> Job: ...
        def _require_public_reservation(
            self,
            operation_id: str | None,
            *,
            participant_id: str,
            kind: ControlKind,
            phase: ControlDeliveryPhase,
            route: ControlRoute,
            job_handle: str | None = None,
        ) -> ControlOperation: ...
        @staticmethod
        def _require_reserved_native_identity(
            operation: ControlOperation, snapshot: RuntimeSnapshot
        ) -> None: ...
        @staticmethod
        def _notify_reserved(
            callback: Callable[[str, str | None], None] | None,
            operation_id: str,
            job_handle: str | None,
        ) -> None: ...
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
        ) -> None: ...
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
        ) -> DeliveryResult | None: ...
        def _receipt_names_operation(self, operation_id: str, receipt: ControlReceipt) -> bool: ...
        def _settle_uncertain(
            self, operation_id: str, *, error: str, execution_barrier: bool | None = None
        ) -> None: ...
        def _settle_from_receipt(
            self,
            operation_id: str,
            receipt: ControlReceipt,
            *,
            execution_barrier: bool | None = None,
            connection=None,
        ) -> None: ...
        def _require_capability(
            self,
            participant_id: str,
            snapshot: RuntimeSnapshot,
            capability: RuntimeCapability,
            action: str,
        ) -> None: ...
        @staticmethod
        def _is_authoritatively_idle(snapshot: RuntimeSnapshot) -> bool: ...
        def _clear_execution_barriers_from_idle_snapshot(
            self, participant_id: str, snapshot: RuntimeSnapshot
        ) -> None: ...
        def _clear_execution_barrier_for_operation(
            self, operation: ControlOperation, *, preserve_deadline: bool = False
        ) -> None: ...
        def _reject_busy(
            self,
            participant_id: str,
            snapshot: RuntimeSnapshot,
            *,
            operation: BusyOperation,
            exclude: str | None = None,
        ) -> None: ...
        def _operation_for_snapshot_turn(self, participant_id: str, snapshot: RuntimeSnapshot): ...
        def _operation_for_turn(
            self,
            *,
            participant_id: str,
            backend_generation: int,
            native_session_id: str,
            native_turn_id: str,
            connection=None,
        ): ...

else:
    ControlHost = object
