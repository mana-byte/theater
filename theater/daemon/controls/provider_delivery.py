"""Exact provider-terminal reservation and callback delivery."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

from theater.daemon.controls.routing import ControlRoute
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
    RuntimeCapability,
)
from theater.models import Busy, JobState, StaleTarget

DELIVERY_UNKNOWN_ERROR_CODE = "delivery_unknown"
SEND_REJECTED_ERROR_CODE = "send_rejected"


class ProviderControlDelivery:
    """Provider-only identity fencing, persistence, and callback dispatch."""

    def __init__(
        self,
        *,
        store: Any,
        jobs: Any,
        gates: Any,
        route_for: Callable[[str, RuntimeCapability], ControlRoute],
        terminal_route_for: Callable[[str], ControlRoute],
        reserve: Callable[..., None],
        clock: Callable[[], float],
        count_unknown: Callable[[ControlKind, str], None],
        notify_settled: Callable[[str], None],
    ) -> None:
        self._store = store
        self._jobs = jobs
        self._gates = gates
        self._route_for = route_for
        self._terminal_route_for = terminal_route_for
        self._reserve = reserve
        self._clock = clock
        self._count_unknown = count_unknown
        self._notify_settled = notify_settled

    def reserve(
        self,
        operation_id: str,
        route: ControlRoute,
        *,
        participant_id: str,
        kind: ControlKind,
        phase: ControlDeliveryPhase,
        job_handle: str | None = None,
        queue_sequence: int | None = None,
        payload: str | None = None,
        connection: Any = None,
    ) -> None:
        terminal = self.require(participant_id, route.capability, route)
        self._reserve(
            operation_id,
            participant_id=participant_id,
            kind=kind,
            transport=ControlTransport.PROVIDER_TERMINAL,
            phase=phase,
            job_handle=job_handle,
            provider_id=terminal.provider_id,
            provider_generation=terminal.provider_generation,
            terminal_id=terminal.terminal_id,
            terminal_incarnation=terminal.terminal_incarnation,
            queue_sequence=queue_sequence,
            payload=payload,
            connection=connection,
        )

    def require(
        self,
        participant_id: str,
        capability: RuntimeCapability,
        expected: ControlRoute,
        *,
        terminal_only: bool = False,
    ) -> Any:
        current = (
            self._terminal_route_for(participant_id)
            if terminal_only
            else self._route_for(participant_id, capability)
        )
        if not current.is_provider or current.terminal is None:
            raise StaleTarget(
                f"provider route for participant {participant_id!r} is no longer bound"
            )
        expected_terminal = expected.terminal

        def identity(terminal: Any) -> tuple[object, ...]:
            return (
                terminal.provider_id,
                terminal.provider_generation,
                terminal.terminal_id,
                terminal.terminal_incarnation,
                terminal.occupant_evidence,
                terminal.process_facts,
            )

        if expected_terminal is None or identity(current.terminal) != identity(expected_terminal):
            raise StaleTarget(
                f"provider terminal identity for participant {participant_id!r} changed"
            )
        if not current.route_available:
            raise Busy(
                f"provider terminal route for participant {participant_id!r} is "
                f"{current.provider_health or current.terminal.health}; wait for exact "
                "generation reconciliation before retrying"
            )
        occupant = current.terminal.occupant_evidence.get("occupant_id")
        if not isinstance(occupant, str) or not occupant:
            raise StaleTarget(
                f"provider terminal route for participant {participant_id!r} lacks "
                "verified occupant evidence"
            )
        return current.terminal

    async def deliver(
        self,
        route: ControlRoute,
        *,
        capability: RuntimeCapability,
        kind: ControlKind,
        participant_id: str,
        control_operation_id: str,
        callback_operation_id: str,
        action: Mapping[str, object] | str,
        job_handle: str | None,
    ) -> DeliveryResult:
        terminal = self.require(participant_id, capability, route)
        dispatch = self._gates.provider_dispatch
        if dispatch is None:
            raise StaleTarget("provider callback transport is not composed")
        self._gates.check_absent(participant_id)
        barrier = kind in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP)
        self._store.mark_control_operation_dispatched(
            control_operation_id,
            provider_id=terminal.provider_id,
            provider_generation=terminal.provider_generation,
            terminal_id=terminal.terminal_id,
            terminal_incarnation=terminal.terminal_incarnation,
            execution_barrier=barrier,
            updated_at=self._clock(),
        )
        params: dict[str, object] = {
            "operation_id": callback_operation_id,
            "provider_generation": terminal.provider_generation,
            "participant_id": participant_id,
            "terminal_id": terminal.terminal_id,
            "terminal_incarnation": terminal.terminal_incarnation,
            "expected_occupant": terminal.occupant_evidence["occupant_id"],
            "action": action,
            "require_absent": True,
        }
        method = "terminal.interrupt" if kind is ControlKind.INTERRUPT else "terminal.deliver"
        try:
            result = await dispatch(
                terminal.provider_id, terminal.provider_generation, method, params
            )
        except asyncio.CancelledError:
            self._settle(
                control_operation_id,
                DeliveryResult.UNKNOWN,
                kind=kind,
                error_code=DELIVERY_UNKNOWN_ERROR_CODE,
                error="provider callback was cancelled after dispatch; outcome is unknown",
            )
            raise
        except Exception as exc:
            details = getattr(exc, "details", None)
            possibly_executed = (
                isinstance(details, Mapping) and details.get("possibly_executed") is True
            )
            delivery = DeliveryResult.UNKNOWN if possibly_executed else DeliveryResult.REJECTED
            self._settle(
                control_operation_id,
                delivery,
                kind=kind,
                error_code=getattr(exc, "code", None) or "dispatch_failed",
                error=str(exc),
            )
            if delivery is DeliveryResult.REJECTED and job_handle is not None:
                self._jobs.finish(
                    job_handle,
                    state=JobState.CRASHED,
                    result=str(exc),
                    error_code=getattr(exc, "code", None) or "dispatch_failed",
                )
            return delivery
        delivery = DeliveryResult(str(result["delivery"]))
        error = result.get("error")
        error_mapping = error if isinstance(error, Mapping) else {}
        self._settle(
            control_operation_id,
            delivery,
            kind=kind,
            error_code=(
                str(error_mapping.get("code")) if error_mapping.get("code") is not None else None
            ),
            error=(
                str(error_mapping.get("message"))
                if error_mapping.get("message") is not None
                else None
            ),
        )
        if delivery is DeliveryResult.REJECTED and job_handle is not None:
            self._jobs.finish(
                job_handle,
                state=JobState.CRASHED,
                result=str(error_mapping.get("message") or "provider rejected delivery"),
                error_code=str(error_mapping.get("code") or SEND_REJECTED_ERROR_CODE),
            )
        return delivery

    def _settle(
        self,
        operation_id: str,
        result: DeliveryResult,
        *,
        kind: ControlKind,
        error_code: str | None,
        error: str | None,
    ) -> None:
        barrier = (
            kind in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP)
            and result is DeliveryResult.UNKNOWN
        )
        self._store.settle_control_operation(
            operation_id,
            result=result,
            error_code=error_code,
            error=error,
            execution_barrier=barrier,
            updated_at=self._clock(),
        )
        if result is DeliveryResult.UNKNOWN:
            self._count_unknown(kind, "ack_lost")
        self._notify_settled(operation_id)


__all__ = ["ProviderControlDelivery"]
