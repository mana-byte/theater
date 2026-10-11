"""Job completion evidence and deferred-task gates."""

from __future__ import annotations

from theater.daemon.handoff.models import HandoffGate, JobEvidence
from theater.daemon.persistence.store_parts._host import StoreHost


class HandoffStore(StoreHost):
    """Store-facing handoff methods; state lives on ``Store``."""

    def put_job_evidence(self, evidence: JobEvidence, *, connection=None) -> None:
        self._handoff.put_evidence(evidence, connection=connection)

    def get_job_evidence(self, job_handle: str, *, connection=None) -> JobEvidence | None:
        return self._handoff.get_evidence(job_handle, connection=connection)

    def delete_job_evidence(self, handles, *, connection=None) -> None:
        self._handoff.delete_evidence(handles, connection=connection)

    def create_handoff_gate(self, gate: HandoffGate, *, connection) -> None:
        self._handoff.create_gate(gate, connection=connection)

    def get_handoff_gate(self, job_handle: str, *, connection=None) -> HandoffGate | None:
        return self._handoff.get_gate(job_handle, connection=connection)

    def handoff_gates(self, states, *, source_handle: str | None = None) -> list[HandoffGate]:
        return self._handoff.gates_in_states(states, source_handle=source_handle)

    def count_open_handoff_gates(self, caller_id: str, *, connection=None) -> int:
        return self._handoff.count_open_for_caller(caller_id, connection=connection)

    def move_handoff_gate(self, job_handle: str, **kwargs) -> bool:
        return self._handoff.move_gate(job_handle, **kwargs)

    def delete_handoff_gates(self, handles, *, connection=None) -> None:
        self._handoff.delete_gates(handles, connection=connection)

    def newest_turn_job(self, target_id: str):
        return self._handoff.newest_turn_job(target_id)

    def job_modified_paths(self, job_handle: str, *, limit: int) -> list[str]:
        return self._handoff.modified_paths(job_handle, limit=limit)
