"""Public control admission: reservations, settlement waits, provider termination."""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping

from theater import timing
from theater.daemon.controls._common import ACTION_TERMINATE, _error_code_of, _operation_row
from theater.daemon.controls._host import ControlHost
from theater.daemon.controls.public_admission import PublicControlReservation
from theater.daemon.controls.routing import ControlRoute
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    RuntimeCapability,
    RuntimeSnapshot,
)
from theater.models import HumanPresent, JobState, StaleTarget
from theater.observability.catalog import LIFECYCLE_STAGE


class AdmissionControls(ControlHost):
    """Reserve, settle, and terminate through the public admission path."""

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
