"""Control operations, queue sequence allocation and native terminal evidence."""

from __future__ import annotations

import json
import logging
from dataclasses import replace

from theater.constants.daemon import (
    PROVIDER_PRESENCE_RETRY_INITIAL_SECONDS,
    PROVIDER_PRESENCE_RETRY_MAX_SECONDS,
)
from theater.daemon.events.publication import control_event, next_revision
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.store_parts._host import StoreHost
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import Job, JobState, new_id

logger = logging.getLogger("theater.store")


class ControlOperationStore(StoreHost):
    """Store-facing controls methods; state lives on ``Store``."""

    def reserve_control_operation(self, operation, *, connection=None) -> None:
        """Persist one control operation before transmission."""
        if connection is not None:
            self._control_operations.reserve(operation, connection=connection)
            return
        with self.write_unit() as unit:
            before = self._control_operations.get(
                operation.operation_id, connection=unit.connection
            )
            self._control_operations.reserve(operation, connection=unit.connection)
            current = self._control_operations.get(
                operation.operation_id, connection=unit.connection
            )
            if current is not None and current != before:
                self._append_control_event(unit, current)

    def get_control_operation(self, operation_id: str, *, connection=None):
        return self._control_operations.get(operation_id, connection=connection)

    def control_operations_for_job(self, job_handle: str) -> list:
        return self._control_operations.for_job(job_handle)

    def control_operation_for_native_turn(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
        connection=None,
    ):
        """Exact native turn -> operation lookup for completion mapping."""
        return self._control_operations.for_native_turn(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            connection=connection,
        )

    def queued_control_operations(self, participant_id: str, *, connection=None) -> list:
        """Queued followups in FIFO order by allocated send sequence."""
        return self._control_operations.queued_for_participant(
            participant_id, connection=connection
        )

    def queued_control_callers(self, participant_id: str, *, connection=None) -> dict:
        """``operation_id -> requesting caller`` for queued rows; lane heads derive from it."""
        return self._control_operations.queued_callers(participant_id, connection=connection)

    def queued_control_count_for_caller(
        self, participant_id: str, caller_id: str, *, connection=None
    ) -> int:
        return self._control_operations.queued_count_for_caller(
            participant_id, caller_id, connection=connection
        )

    def set_queued_control_payload(
        self, operation_id: str, payload: str, *, connection=None
    ) -> None:
        """Persist a queued operation's bounded causal context, not a turn binding."""
        self._control_operations.set_queued_payload(operation_id, payload, connection=connection)

    def set_queued_control_route(
        self,
        operation_id: str,
        *,
        transport,
        backend_generation: int | None,
        native_session_id: str | None,
        payload: str | None,
        updated_at: float,
        connection=None,
    ) -> bool:
        """Persist a dispatch-selected transport while a followup is still queued."""
        if connection is not None:
            return self._control_operations.set_queued_route(
                operation_id,
                transport=transport,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                payload=payload,
                updated_at=updated_at,
                connection=connection,
            )
        with self.write_unit() as unit:
            changed = self._control_operations.set_queued_route(
                operation_id,
                transport=transport,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                payload=payload,
                updated_at=updated_at,
                connection=unit.connection,
            )
            current = self._control_operations.get(operation_id, connection=unit.connection)
            if changed and current is not None:
                self._append_control_event(unit, current)
            return changed

    def dispatched_control_operations(self, participant_id: str) -> list:
        """Operations whose transmission began and whose ack may never arrive."""
        return self._control_operations.dispatched_for_participant(participant_id)

    def execution_barrier_control_operations(self, participant_id: str) -> list:
        """Native prompt operations whose execution is still unresolved."""
        return self._control_operations.execution_barriers_for_participant(participant_id)

    def has_execution_barrier(self, participant_id: str, *, connection=None) -> bool:
        """Whether unresolved native execution blocks automated prompts."""
        return self._control_operations.has_execution_barrier(participant_id, connection=connection)

    def unresolved_prompt_delivery_operations(self, participant_id: str) -> list:
        """Prompt rows still awaiting exact evidence or their deadline."""
        return self._control_operations.unresolved_prompt_deliveries_for_participant(participant_id)

    def control_operations_in_phases(self, participant_id: str, phases) -> list:
        """Every operation still in the given phases — the restart enumeration."""
        return self._control_operations.in_phases(participant_id, phases)

    def queued_control_operation_count(self, participant_id: str, *, connection=None) -> int:
        return self._control_operations.pending_count_for_participant(
            participant_id, connection=connection
        )

    def mark_control_operation_dispatched(
        self,
        operation_id: str,
        *,
        native_session_id: str | None = None,
        native_turn_id: str | None = None,
        provider_id: str | None = None,
        provider_generation: int | None = None,
        terminal_id: str | None = None,
        terminal_incarnation: str | None = None,
        execution_barrier: bool | None = None,
        updated_at: float,
        connection=None,
    ) -> None:
        if connection is not None:
            self._control_operations.mark_dispatched(
                operation_id,
                native_session_id=native_session_id,
                native_turn_id=native_turn_id,
                provider_id=provider_id,
                provider_generation=provider_generation,
                terminal_id=terminal_id,
                terminal_incarnation=terminal_incarnation,
                execution_barrier=execution_barrier,
                updated_at=updated_at,
                connection=connection,
            )
            return
        with self.write_unit() as unit:
            self._control_operations.mark_dispatched(
                operation_id,
                native_session_id=native_session_id,
                native_turn_id=native_turn_id,
                provider_id=provider_id,
                provider_generation=provider_generation,
                terminal_id=terminal_id,
                terminal_incarnation=terminal_incarnation,
                execution_barrier=execution_barrier,
                updated_at=updated_at,
                connection=unit.connection,
            )
            current = self._control_operations.get(operation_id, connection=unit.connection)
            if current is not None:
                self._append_control_event(unit, current)

    def add_settlement_listener(self, listener) -> None:
        """Run ``listener`` inside every delivery settlement's own transaction."""
        self._settlement_listeners.append(listener)

    def _notify_settled(self, operation_id: str, result, connection) -> None:
        for listener in tuple(self._settlement_listeners):
            try:
                listener(operation_id, result, connection)
            except Exception:
                logger.exception("settlement listener failed for operation %s", operation_id)

    def settle_control_operation(
        self,
        operation_id: str,
        *,
        result,
        native_turn_id: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        execution_barrier: bool | None = None,
        updated_at: float,
        connection=None,
    ) -> None:
        if connection is not None:
            self._control_operations.settle(
                operation_id,
                result=result,
                native_turn_id=native_turn_id,
                error_code=error_code,
                error=error,
                execution_barrier=execution_barrier,
                updated_at=updated_at,
                connection=connection,
            )
            self._notify_settled(operation_id, result, connection)
            return
        with self.write_unit() as unit:
            before = self._control_operations.get(operation_id, connection=unit.connection)
            self._control_operations.settle(
                operation_id,
                result=result,
                native_turn_id=native_turn_id,
                error_code=error_code,
                error=error,
                execution_barrier=execution_barrier,
                updated_at=updated_at,
                connection=unit.connection,
            )
            current = self._control_operations.get(operation_id, connection=unit.connection)
            if current is not None and current != before:
                self._append_control_event(unit, current)
            self._notify_settled(operation_id, result, unit.connection)

    def defer_provider_presence(
        self, operation_id: str, *, error: str, updated_at: float
    ) -> ControlOperation:
        """Queue a proved unsent prompt with a new callback ID in one transaction."""
        with self.write_unit() as unit:
            old = self._control_operations.get(operation_id, connection=unit.connection)
            if (
                old is None
                or old.transport is not ControlTransport.PROVIDER_TERMINAL
                or old.delivery_phase is not ControlDeliveryPhase.DISPATCHED
                or old.kind not in (ControlKind.SEND, ControlKind.QUEUE_FOLLOWUP)
                or old.job_handle is None
            ):
                raise RuntimeError("provider presence deferral needs a dispatched prompt")
            job = self._jobs.get(old.job_handle, connection=unit.connection)
            if job is None or job.state != JobState.RUNNING:
                raise RuntimeError("provider presence deferral lost its running job")
            retry_id = f"{old.job_handle}:presence:{new_id()}"
            payload = json.loads(old.payload or "{}")
            refusals = payload.get("presence_refusals")
            count = min((refusals if type(refusals) is int and refusals > 0 else 0) + 1, 5)
            delay = min(
                PROVIDER_PRESENCE_RETRY_INITIAL_SECONDS * 2 ** (count - 1),
                PROVIDER_PRESENCE_RETRY_MAX_SECONDS,
            )
            payload["presence_refusals"] = count
            payload["presence_retry_after"] = updated_at + delay
            if old.kind is ControlKind.SEND:
                payload["origin"] = "send"
            payload["callback_operation_id"] = retry_id
            sequence = old.queue_sequence or self.allocate_control_queue_sequence(
                connection=unit.connection
            )
            self._control_operations.settle(
                operation_id,
                result=DeliveryResult.REJECTED,
                error_code="human_present",
                error=error,
                updated_at=updated_at,
                connection=unit.connection,
            )
            settled = self._control_operations.get(operation_id, connection=unit.connection)
            assert settled is not None
            queued = replace(
                old,
                operation_id=retry_id,
                kind=ControlKind.QUEUE_FOLLOWUP,
                delivery_phase=ControlDeliveryPhase.QUEUED,
                delivery_result=None,
                execution_barrier=False,
                native_turn_id=None,
                queue_sequence=sequence,
                payload=json.dumps(payload),
                error_code=None,
                error=None,
                created_at=updated_at,
                updated_at=updated_at,
            )
            self._control_operations.reserve(queued, connection=unit.connection)
            if self._control_operations.get(retry_id, connection=unit.connection) != queued:
                raise RuntimeError("provider presence deferral could not reserve its queued retry")
            self._append_control_event(unit, settled)
            self._append_control_event(unit, queued)
        return queued

    def set_control_execution_barrier(
        self,
        operation_id: str,
        *,
        active: bool,
        updated_at: float,
        connection=None,
    ) -> None:
        """Persist whether an uncertain native prompt still blocks delivery."""
        if connection is not None:
            self._control_operations.set_execution_barrier(
                operation_id,
                active=active,
                updated_at=updated_at,
                connection=connection,
            )
            return
        with self.write_unit() as unit:
            before = self._control_operations.get(operation_id, connection=unit.connection)
            self._control_operations.set_execution_barrier(
                operation_id,
                active=active,
                updated_at=updated_at,
                connection=unit.connection,
            )
            current = self._control_operations.get(operation_id, connection=unit.connection)
            if current is not None and current != before:
                self._append_control_event(unit, current)

    def _append_control_event(self, unit, operation) -> None:
        event = control_event(
            self,
            operation,
            unit.connection,
            revision=next_revision(self, unit.connection),
        )
        if event is not None:
            self.journal.append_group(unit, [event])

    def active_running_jobs_for_target(self, target_id: str, *, connection=None) -> list[Job]:
        """Running jobs actually dispatched to the target, oldest first."""
        return self._control_operations.active_running_for_target(target_id, connection=connection)

    def completion_candidate_jobs_for_target(self, target_id: str, *, connection=None) -> list[Job]:
        """Delivered running jobs a completion matcher may finish, oldest first.

        A job awaiting an internal repair answer stays in ``active_running_jobs_for_target`` for
        every safety query, but no heuristic matcher may treat a later turn as its answer.
        """
        delivered = self._control_operations.active_running_for_target(
            target_id, connection=connection
        )
        repairing = self._repairs.repairing(
            [job.handle for job in delivered], connection=connection
        )
        return [job for job in delivered if job.handle not in repairing]

    def allocate_control_queue_sequence(self, *, connection=None) -> int:
        """One queue position from the persisted send-sequence allocator."""
        return self._meta.allocate_send_seq(connection=connection)

    def get_control_queue_sequence(self, *, connection=None) -> int:
        """Current persisted send-sequence allocator value."""
        return self._meta.get_send_seq(connection=connection)

    def prune_control_operations(
        self, *, older_than: float, limit: int | None = None, connection=None
    ) -> int:
        """Bounded prune of settled operations."""
        kwargs: dict = {"older_than": older_than}
        if limit is not None:
            kwargs["limit"] = limit
        kwargs["connection"] = connection
        return self._control_operations.prune(**kwargs)

    def record_native_terminal_evidence(self, evidence, *, connection=None) -> bool:
        """Persist terminal evidence; first write wins."""
        return self._native_evidence.record(evidence, connection=connection)

    def get_native_terminal_evidence(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
    ):
        return self._native_evidence.get(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
        )

    def native_terminal_evidence_for_participant(self, participant_id: str) -> list:
        return self._native_evidence.for_participant(participant_id)

    def prune_native_terminal_evidence(
        self, *, older_than: float, limit: int | None = None, connection=None
    ) -> int:
        """Bounded prune of terminal evidence."""
        kwargs: dict = {"older_than": older_than}
        if limit is not None:
            kwargs["limit"] = limit
        kwargs["connection"] = connection
        return self._native_evidence.prune(**kwargs)
