"""Strict response contracts: the custom registry, repair attempts and their fence, the audit."""

from __future__ import annotations

from theater.daemon.persistence.repositories.job_repairs import RepairAttempt
from theater.daemon.persistence.store_parts._host import StoreHost


class ContractStore(StoreHost):
    """Store-facing contract methods; state lives on ``Store``."""

    # ---- custom registry --------------------------------------------------

    def get_response_contract(self, creator_id: str, name: str) -> str | None:
        return self._response_contracts.get(creator_id, name)

    def register_response_contract(
        self, creator_id: str, name: str, schema: str, *, created_at: float, limit: int
    ) -> str:
        with self.write_unit() as unit:
            return self._response_contracts.register(
                creator_id,
                name,
                schema,
                created_at=created_at,
                limit=limit,
                connection=unit.connection,
            )

    def delete_response_contracts(self, creator_id: str) -> None:
        self._response_contracts.delete_for_creator(creator_id)

    # ---- repair attempts and the fence ------------------------------------

    def create_repair_attempt(self, attempt: RepairAttempt, *, connection=None) -> bool:
        return self._repairs.create(attempt, connection=connection)

    def get_repair_attempt(self, attempt_id: str, *, connection=None) -> RepairAttempt | None:
        return self._repairs.get(attempt_id, connection=connection)

    def repair_attempts_for_job(self, job_handle: str, *, connection=None) -> list[RepairAttempt]:
        return self._repairs.for_job(job_handle, connection=connection)

    def open_repair_attempts(self, participant_id: str, *, connection=None) -> list[RepairAttempt]:
        return self._repairs.open_for_participant(participant_id, connection=connection)

    def repair_fence_active(self, participant_id: str, *, connection=None) -> bool:
        """Whether an unresolved repair still forbids new prompts to this participant."""
        return self._repairs.fence_active(participant_id, connection=connection)

    def repair_attempt_for_turn(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
        connection=None,
    ) -> RepairAttempt | None:
        return self._repairs.for_native_turn(
            participant_id=participant_id,
            backend_generation=backend_generation,
            native_session_id=native_session_id,
            native_turn_id=native_turn_id,
            connection=connection,
        )

    def job_is_repairing(self, job_handle: str, *, connection=None) -> bool:
        return bool(self._repairs.repairing([job_handle], connection=connection))

    def update_repair_attempt(self, attempt_id: str, *, connection=None, **changes) -> None:
        self._repairs.update(attempt_id, connection=connection, **changes)

    # ---- audit -------------------------------------------------------------

    def record_contract_audit(
        self,
        job_handle: str,
        participant_id: str | None,
        event: str,
        detail: str | None,
        *,
        at: float,
        connection=None,
    ) -> None:
        self._repairs.add_audit(
            job_handle, participant_id, event, detail, at=at, connection=connection
        )

    def contract_audit_for_job(self, job_handle: str) -> list[tuple[str, str | None]]:
        return self._repairs.audit_for_job(job_handle)
