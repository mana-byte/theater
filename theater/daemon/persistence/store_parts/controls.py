"""Control operations, queue sequence allocation and native terminal evidence."""

from __future__ import annotations

from theater.daemon.events.publication import control_event, next_revision
from theater.daemon.persistence.store_parts._host import StoreHost
from theater.models import Job


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

    def allocate_control_queue_sequence(self, *, connection=None) -> int:
        """One queue position from the persisted send-sequence allocator."""
        return self._meta.allocate_send_seq(connection=connection)

    def get_control_queue_sequence(self, *, connection=None) -> int:
        """Current persisted send-sequence allocator value."""
        return self._meta.get_send_seq(connection=connection)

    def prune_control_operations(self, *, older_than: float, limit: int | None = None) -> int:
        """Bounded prune of settled operations."""
        kwargs: dict = {"older_than": older_than}
        if limit is not None:
            kwargs["limit"] = limit
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

    def prune_native_terminal_evidence(self, *, older_than: float, limit: int | None = None) -> int:
        """Bounded prune of terminal evidence."""
        kwargs: dict = {"older_than": older_than}
        if limit is not None:
            kwargs["limit"] = limit
        return self._native_evidence.prune(**kwargs)
