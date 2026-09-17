"""Atomic reconciliation of historical provider receipts."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from jsonschema.exceptions import ValidationError
from sqlalchemy import update

from theater.daemon.operations.projection import operation_event_payload
from theater.daemon.operations.service import IDEMPOTENCY_RETENTION_SECONDS
from theater.daemon.schema import launch_reservations
from theater.frontend.schemas import validator_for
from theater.harness.contracts.runtime import ControlDeliveryPhase, DeliveryResult
from theater.models import (
    Job,
    JobState,
    JournalEventRecord,
    PublicOperationRecord,
    PublicOperationState,
    Status,
    TerminalBindingRecord,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    new_id,
)

_TERMINAL_IDENTITY_SCHEMA = (
    "https://theater.dev/schemas/frontend/1.0/common.json#/$defs/terminalIdentity"
)


class ProviderReceiptError(ValueError):
    pass


class ProviderReceiptReconciler:
    """Turn current-generation reports into exact, non-replayed settlements."""

    def __init__(self, store, operations) -> None:
        self._store = store
        self._operations = operations
        self._controls: Any = None
        self._jobs: Any = None

    def configure_runtime(self, *, controls: Any, jobs: Any) -> None:
        self._controls = controls
        self._jobs = jobs

    def validate(
        self,
        provider_id: str,
        receipts: Sequence[Mapping[str, object]],
    ) -> None:
        for receipt in receipts:
            operation_id = receipt.get("operation_id")
            if not isinstance(operation_id, str):
                raise ProviderReceiptError("provider receipts require an operation_id")
            operation = self._operations.get(operation_id)
            identity = self._identity(receipt)
            reported_provider = identity.get("provider_id", provider_id)
            expected = (
                operation.dispatch_provider_id,
                operation.dispatch_provider_generation,
            )
            reported = (reported_provider, identity.get("provider_generation"))
            if expected != reported:
                raise ProviderReceiptError("historical receipt changed its provider generation")
            outcome = receipt.get("outcome", receipt.get("delivery"))
            if outcome not in {"accepted", "rejected", "unknown"}:
                raise ProviderReceiptError(
                    "provider receipt outcome must be accepted, rejected, or unknown"
                )
            if operation.kind == "spawn":
                self._validate_launch_receipt(operation, receipt, identity)
                continue
            terminal = (
                operation.dispatch_terminal_id,
                operation.dispatch_terminal_incarnation,
            )
            if any(value is not None for value in terminal) and terminal != (
                identity.get("terminal_id"),
                identity.get("terminal_incarnation"),
            ):
                raise ProviderReceiptError("historical receipt changed its terminal identity")
            nested = receipt.get("terminal")
            if isinstance(nested, Mapping):
                self._validate_terminal_identity(nested)
                if (
                    operation.dispatch_terminal_occupant_evidence is not None
                    and nested.get("occupant") != operation.dispatch_terminal_occupant_evidence
                ):
                    raise ProviderReceiptError("historical receipt changed its occupant evidence")
                if (
                    operation.dispatch_terminal_process_facts is not None
                    and nested.get("process") != operation.dispatch_terminal_process_facts
                ):
                    raise ProviderReceiptError("historical receipt changed its process evidence")

    def reconcile(
        self,
        unit,
        *,
        provider_id: str,
        current_generation: int,
        report_revision: int,
        inventory_complete: bool,
        terminals: Sequence[Mapping[str, object]],
        receipts: Sequence[Mapping[str, object]],
        timestamp: float,
        first_revision: int,
    ) -> tuple[tuple[str, ...], tuple[JournalEventRecord, ...]]:
        reconciled: list[str] = []
        events: list[JournalEventRecord] = []
        for receipt in receipts:
            operation_id = str(receipt["operation_id"])
            operation = self._store.operations.get(operation_id, connection=unit.connection)
            if operation is None or operation.state not in {
                PublicOperationState.RUNNING.value,
                PublicOperationState.UNCERTAIN.value,
            }:
                continue
            outcome = receipt.get("outcome", receipt.get("delivery"))
            if outcome == "unknown" or operation.kind == "participants.terminate":
                continue
            changed: list[JournalEventRecord] = []
            if operation.kind == "spawn":
                if outcome == "accepted":
                    if not inventory_complete:
                        continue
                    self._recover_spawn(
                        operation,
                        receipt,
                        terminals,
                        provider_id=provider_id,
                        current_generation=current_generation,
                        report_revision=report_revision,
                        timestamp=timestamp,
                        unit=unit,
                        events=changed,
                    )
                    operation = self._spawn_dispatch_identity(operation, receipt)
                    result: object = {
                        "participant_id": operation.target_ids[0],
                        "job_handle": operation.job_handle,
                    }
                    error = None
                    state = PublicOperationState.SUCCEEDED.value
                else:
                    self._rollback_spawn(operation, timestamp=timestamp, unit=unit, events=changed)
                    result = None
                    error = self._error(receipt)
                    state = PublicOperationState.FAILED.value
            else:
                result = {"delivery": "accepted"} if outcome == "accepted" else None
                error = None if outcome == "accepted" else self._error(receipt)
                state = (
                    PublicOperationState.SUCCEEDED.value
                    if outcome == "accepted"
                    else PublicOperationState.FAILED.value
                )
                self._settle_control(
                    operation,
                    outcome=str(outcome),
                    error=error,
                    timestamp=timestamp,
                    unit=unit,
                    events=changed,
                )
            updated_at = math.nextafter(max(timestamp, operation.updated_at), math.inf)
            updated = replace(
                operation,
                state=state,
                phase="provider_receipt_reconciled",
                result=result,
                error=error,
                error_code=None if error is None else str(error["code"]),
                updated_at=updated_at,
                settled_at=updated_at,
            )
            if not self._store.operations.replace(
                updated,
                expected_state=operation.state,
                expected_updated_at=operation.updated_at,
                connection=unit.connection,
            ):
                continue
            self._store.operations.settle_idempotency_for_operation(
                operation_id,
                settled_at=updated_at,
                retain_until=updated_at + IDEMPOTENCY_RETENTION_SECONDS,
                connection=unit.connection,
            )
            changed.append(
                JournalEventRecord(
                    kind="operation.updated",
                    entity_id=operation_id,
                    entity_revision=0,
                    payload=operation_event_payload(updated),
                    recorded_at=updated_at,
                )
            )
            for event in changed:
                events.append(replace(event, entity_revision=first_revision + len(events)))
            unit.after_commit(
                lambda operation_id=operation_id: self._operations.notify_persisted_change(
                    operation_id
                )
            )
            reconciled.append(operation_id)
        return tuple(reconciled), tuple(events)

    def _recover_spawn(
        self,
        operation: PublicOperationRecord,
        receipt: Mapping[str, object],
        terminals: Sequence[Mapping[str, object]],
        *,
        provider_id: str,
        current_generation: int,
        report_revision: int,
        timestamp: float,
        unit,
        events: list[JournalEventRecord],
    ) -> None:
        launch = self._store.operations.get_launch(
            operation.operation_id, connection=unit.connection
        )
        if launch is None or launch.dispatch_marker is None:
            raise ProviderReceiptError("terminal creation receipt has no durable dispatch marker")
        receipt_terminal = receipt.get("terminal")
        assert isinstance(receipt_terminal, Mapping)
        candidate = self._current_terminal(
            terminals,
            receipt_terminal,
            provider_id=provider_id,
            generation=current_generation,
            launch_id=launch.operation_id,
        )
        participant_id = launch.participant_id
        existing = self._store.terminal_bindings.get(participant_id, connection=unit.connection)
        binding = self._binding(
            participant_id,
            candidate,
            report_revision=report_revision,
            timestamp=timestamp,
        )
        binding_created = existing is None
        if existing is None:
            for other in self._store.terminal_bindings.list_for_provider(
                provider_id, connection=unit.connection
            ):
                if other.terminal_id == binding.terminal_id:
                    raise ProviderReceiptError("reclaimed terminal is bound to another participant")
            self._store.terminal_bindings.bind(binding, connection=unit.connection)
        elif self._binding_key(existing) != self._binding_key(binding):
            raise ProviderReceiptError("reclaimed terminal does not match its durable binding")

        if launch.workspace_usage_id is not None:
            usage = self._store.workspaces.get_usage(
                launch.workspace_usage_id, connection=unit.connection
            )
            if usage is not None and usage.released_at is None:
                participant_usage = WorkspaceUsageRecord(
                    usage_id=new_id(),
                    workspace_id=usage.workspace_id,
                    holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                    holder_id=participant_id,
                    acquired_at=timestamp,
                )
                self._store.workspaces.handoff_usage(
                    reservation_usage_id=usage.usage_id,
                    participant_usage=participant_usage,
                    handed_off_at=timestamp,
                    connection=unit.connection,
                )
                events.append(self._usage_event(participant_usage, timestamp, "handed_off"))
        unit.connection.execute(
            update(launch_reservations)
            .where(launch_reservations.c.operation_id == operation.operation_id)
            .values(phase="terminal_bound", updated_at=timestamp)
        )
        if binding_created:
            events.append(self._binding_event(binding, timestamp))

    def _rollback_spawn(
        self,
        operation: PublicOperationRecord,
        *,
        timestamp: float,
        unit,
        events: list[JournalEventRecord],
    ) -> None:
        launch = self._store.operations.get_launch(
            operation.operation_id, connection=unit.connection
        )
        if launch is None or launch.dispatch_marker is None:
            raise ProviderReceiptError("rejected creation receipt has no durable dispatch marker")
        participant = self._store.get_participant(launch.participant_id, connection=unit.connection)
        if participant is not None and participant.status is not Status.DEAD:
            participant = replace(
                participant,
                status=Status.DEAD,
                termination_reason="spawn_failed",
                terminated_at=timestamp,
                last_activity=timestamp,
            )
            self._store.upsert_participant(participant, connection=unit.connection)
            events.append(
                JournalEventRecord(
                    kind="participant.updated",
                    entity_id=participant.id,
                    entity_revision=0,
                    payload={
                        "participant_id": participant.id,
                        "status": Status.DEAD.value,
                        "parent_id": participant.parent_id,
                        "workspace_id": participant.workspace_id,
                    },
                    recorded_at=timestamp,
                )
            )
        self._finish_job(
            operation.job_handle,
            state=JobState.CRASHED,
            error_code="provider_unavailable",
            result="provider receipt proved terminal creation was rejected",
            timestamp=timestamp,
            unit=unit,
            events=events,
        )
        if launch.workspace_usage_id is not None:
            usage = self._store.workspaces.get_usage(
                launch.workspace_usage_id, connection=unit.connection
            )
            if usage is not None and usage.released_at is None:
                self._store.workspaces.release_usage(
                    usage.usage_id,
                    released_at=timestamp,
                    reason="launch_rolled_back",
                    connection=unit.connection,
                )
                events.append(self._usage_event(usage, timestamp, "released"))
        unit.connection.execute(
            update(launch_reservations)
            .where(launch_reservations.c.operation_id == operation.operation_id)
            .values(phase="rolled_back", updated_at=timestamp)
        )

    def _settle_control(
        self,
        operation: PublicOperationRecord,
        *,
        outcome: str,
        error: Mapping[str, object] | None,
        timestamp: float,
        unit,
        events: list[JournalEventRecord],
    ) -> None:
        if operation.control_operation_id is None:
            return
        control = self._store.get_control_operation(
            operation.control_operation_id, connection=unit.connection
        )
        if control is None or control.delivery_phase is ControlDeliveryPhase.QUEUED:
            raise ProviderReceiptError("historical receipt does not name dispatched control")
        if (
            control.provider_id,
            control.provider_generation,
            control.terminal_id,
            control.terminal_incarnation,
        ) != (
            operation.dispatch_provider_id,
            operation.dispatch_provider_generation,
            operation.dispatch_terminal_id,
            operation.dispatch_terminal_incarnation,
        ):
            raise ProviderReceiptError("control dispatch identity does not match public operation")
        result = DeliveryResult.ACCEPTED if outcome == "accepted" else DeliveryResult.REJECTED
        self._store.settle_control_operation(
            control.operation_id,
            result=result,
            error_code=None if error is None else str(error["code"]),
            error=None if error is None else str(error["message"]),
            execution_barrier=False,
            updated_at=timestamp,
            connection=unit.connection,
        )
        if self._controls is not None:
            unit.after_commit(
                lambda operation_id=control.operation_id: (
                    self._controls.notify_persisted_settlement(operation_id)
                )
            )
        if result is DeliveryResult.REJECTED:
            self._finish_job(
                control.job_handle,
                state=JobState.CRASHED,
                error_code=str(error["code"]) if error is not None else "provider_unavailable",
                result=(
                    str(error["message"])
                    if error is not None
                    else "provider receipt proved delivery was rejected"
                ),
                timestamp=timestamp,
                unit=unit,
                events=events,
            )

    def _finish_job(
        self,
        handle: str | None,
        *,
        state: JobState,
        error_code: str,
        result: str,
        timestamp: float,
        unit,
        events: list[JournalEventRecord],
    ) -> None:
        if handle is None:
            return
        job = self._store.get_job(handle, connection=unit.connection)
        if job is None or job.state != JobState.RUNNING:
            return
        finished = replace(
            job,
            state=state.value,
            result=result,
            error_code=error_code,
            finished_at=timestamp,
        )
        self._store.finish_job(
            handle,
            state=finished.state,
            result=finished.result,
            error_code=finished.error_code,
            finished_at=timestamp,
            response_format=finished.response_format,
            structured_result=finished.structured_result,
            structured_status=finished.structured_status,
            connection=unit.connection,
        )
        events.append(self._job_event(finished, timestamp))
        if self._jobs is not None:
            unit.after_commit(
                lambda handle=handle, state=state, error_code=error_code: self._jobs.finish(
                    handle, state=state, error_code=error_code
                )
            )

    def _validate_launch_receipt(
        self,
        operation: PublicOperationRecord,
        receipt: Mapping[str, object],
        identity: Mapping[str, object],
    ) -> None:
        launch = self._store.operations.get_launch(operation.operation_id)
        if launch is None or launch.dispatch_marker is None:
            raise ProviderReceiptError("terminal creation receipt has no dispatched launch")
        if receipt.get("launch_id") != launch.operation_id:
            raise ProviderReceiptError("historical receipt changed its launch identity")
        terminal = receipt.get("terminal")
        if receipt.get("outcome") == "accepted":
            if not isinstance(terminal, Mapping):
                raise ProviderReceiptError("accepted terminal creation receipt needs an identity")
            self._validate_terminal_identity(terminal)
            occupant = terminal.get("occupant")
            if (
                not isinstance(occupant, Mapping)
                or occupant.get("occupant_id") != launch.participant_id
            ):
                raise ProviderReceiptError("terminal creation receipt changed its participant")
        elif identity.get("provider_generation") != operation.dispatch_provider_generation:
            raise ProviderReceiptError("terminal creation receipt changed its generation")

    @staticmethod
    def _identity(receipt: Mapping[str, object]) -> Mapping[str, object]:
        terminal = receipt.get("terminal")
        return terminal if isinstance(terminal, Mapping) else receipt

    @staticmethod
    def _spawn_dispatch_identity(
        operation: PublicOperationRecord, receipt: Mapping[str, object]
    ) -> PublicOperationRecord:
        terminal = receipt["terminal"]
        assert isinstance(terminal, Mapping)
        occupant = terminal["occupant"]
        process = terminal.get("process")
        generation = terminal["provider_generation"]
        assert isinstance(occupant, Mapping)
        assert process is None or isinstance(process, Mapping)
        if type(generation) is not int:
            raise ProviderReceiptError("terminal generation is not an integer")
        return replace(
            operation,
            dispatch_terminal_id=str(terminal["terminal_id"]),
            dispatch_terminal_incarnation=str(terminal["terminal_incarnation"]),
            dispatch_terminal_occupant_evidence=dict(occupant),
            dispatch_terminal_process_facts=None if process is None else dict(process),
            dispatch_provider_generation=generation,
        )

    @staticmethod
    def _validate_terminal_identity(identity: Mapping[str, object]) -> None:
        try:
            validator_for(_TERMINAL_IDENTITY_SCHEMA).validate(identity)
        except ValidationError as exc:
            raise ProviderReceiptError(
                f"provider receipt terminal identity is invalid: {exc}"
            ) from exc

    @staticmethod
    def _current_terminal(
        terminals: Sequence[Mapping[str, object]],
        receipt_terminal: Mapping[str, object],
        *,
        provider_id: str,
        generation: int,
        launch_id: str,
    ) -> Mapping[str, object]:
        for terminal in terminals:
            if (
                terminal.get("provider_id") == provider_id
                and terminal.get("provider_generation") == generation
                and terminal.get("terminal_id") == receipt_terminal.get("terminal_id")
                and terminal.get("terminal_incarnation")
                == receipt_terminal.get("terminal_incarnation")
                and terminal.get("occupant") == receipt_terminal.get("occupant")
                and terminal.get("process") == receipt_terminal.get("process")
                and terminal.get("launch_id") == launch_id
            ):
                return terminal
        raise ProviderReceiptError(
            "terminal creation receipt is not corroborated by current complete inventory"
        )

    @staticmethod
    def _binding(
        participant_id: str,
        terminal: Mapping[str, object],
        *,
        report_revision: int,
        timestamp: float,
    ) -> TerminalBindingRecord:
        occupant = terminal["occupant"]
        process = terminal.get("process")
        generation = terminal["provider_generation"]
        assert isinstance(occupant, Mapping)
        assert process is None or isinstance(process, Mapping)
        if type(generation) is not int:
            raise ProviderReceiptError("terminal generation is not an integer")
        return TerminalBindingRecord(
            participant_id=participant_id,
            provider_id=str(terminal["provider_id"]),
            provider_generation=generation,
            terminal_id=str(terminal["terminal_id"]),
            terminal_incarnation=str(terminal["terminal_incarnation"]),
            occupant_evidence=dict(occupant),
            process_facts=None if process is None else dict(process),
            health="healthy",
            report_revision=report_revision,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @staticmethod
    def _binding_key(binding: TerminalBindingRecord) -> tuple[object, ...]:
        return (
            binding.provider_id,
            binding.provider_generation,
            binding.terminal_id,
            binding.terminal_incarnation,
            binding.occupant_evidence,
            binding.process_facts,
        )

    @staticmethod
    def _error(receipt: Mapping[str, object]) -> dict[str, object]:
        value = receipt.get("error")
        if isinstance(value, Mapping):
            code = value.get("code")
            message = value.get("message")
            if isinstance(code, str) and code and isinstance(message, str):
                return {"code": code[:512], "message": message[:8192]}
        return {
            "code": "provider_unavailable",
            "message": "provider receipt proves the terminal request was rejected",
        }

    @staticmethod
    def _binding_event(binding: TerminalBindingRecord, timestamp: float) -> JournalEventRecord:
        return JournalEventRecord(
            kind="terminal.binding_changed",
            entity_id=binding.participant_id,
            entity_revision=0,
            payload={
                "provider_id": binding.provider_id,
                "provider_generation": binding.provider_generation,
                "terminal_id": binding.terminal_id,
                "terminal_incarnation": binding.terminal_incarnation,
                "occupant": dict(binding.occupant_evidence),
                "process": None if binding.process_facts is None else dict(binding.process_facts),
                "participant_id": binding.participant_id,
                "health": binding.health,
                "report_revision": binding.report_revision,
            },
            recorded_at=timestamp,
        )

    @staticmethod
    def _usage_event(
        usage: WorkspaceUsageRecord, timestamp: float, action: str
    ) -> JournalEventRecord:
        return JournalEventRecord(
            kind="workspace.usage_changed",
            entity_id=usage.workspace_id,
            entity_revision=0,
            payload={
                "workspace_id": usage.workspace_id,
                "usage_id": usage.usage_id,
                "holder_kind": usage.holder_kind,
                "holder_id": usage.holder_id,
                "acquired_at": usage.acquired_at,
                "action": action,
            },
            recorded_at=timestamp,
        )

    @staticmethod
    def _job_event(job: Job, timestamp: float) -> JournalEventRecord:
        return JournalEventRecord(
            kind="job.updated",
            entity_id=job.handle,
            entity_revision=0,
            payload={
                "handle": job.handle,
                "state": str(job.state),
                "kind": str(job.kind),
                "target_id": job.target_id,
                "error": {"code": job.error_code, "message": job.result or ""},
            },
            recorded_at=timestamp,
        )


__all__ = ["ProviderReceiptError", "ProviderReceiptReconciler"]
