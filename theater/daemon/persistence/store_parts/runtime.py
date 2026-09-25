"""Native runtime bindings and participant controls-changed journaling."""

from __future__ import annotations

from theater.daemon.events.publication import next_revision, participant_event
from theater.daemon.persistence.store_parts._host import StoreHost
from theater.models import now


class RuntimeBindingStore(StoreHost):
    """Store-facing runtime methods; state lives on ``Store``."""

    def runtime_transaction(self):
        """One explicit transaction for runtime-storage write boundaries."""
        return self.engine.begin()

    def upsert_runtime_binding(self, binding, *, connection=None) -> None:
        """Idempotently persist one participant runtime binding row."""
        if connection is not None:
            self._runtime_bindings.upsert(binding, connection=connection)
            return
        with self.write_unit() as unit:
            before = self._runtime_bindings.get(binding.participant_id, connection=unit.connection)
            self._runtime_bindings.upsert(binding, connection=unit.connection)
            current = self._runtime_bindings.get(binding.participant_id, connection=unit.connection)
            if current != before:
                self._append_participant_controls_event(
                    unit, binding.participant_id, recorded_at=binding.updated_at
                )

    def get_runtime_binding(self, participant_id: str, *, connection=None):
        return self._runtime_bindings.get(participant_id, connection=connection)

    def runtime_binding_by_native_session(self, native_session_id: str):
        """Exact identity lookup; the only non-heuristic session search."""
        return self._runtime_bindings.find_by_native_session(native_session_id)

    def runtime_bindings_for_recovery(self) -> list:
        """Bindings a daemon restart must reconcile before assuming loss."""
        return self._runtime_bindings.list_recoverable()

    def mark_runtime_backend_started(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        pid: int,
        started_at: float,
        connection=None,
    ) -> bool:
        """Record a verified backend pid, guarded by the expected generation."""
        return self._runtime_bindings.mark_backend_started(
            participant_id,
            backend_generation=backend_generation,
            pid=pid,
            started_at=started_at,
            connection=connection,
        )

    def record_runtime_endpoint(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        endpoint: str,
        updated_at: float,
        connection=None,
    ) -> bool:
        """Persist one generation's discovered endpoint, generation-guarded."""
        return self._runtime_bindings.record_discovered_endpoint(
            participant_id,
            backend_generation=backend_generation,
            endpoint=endpoint,
            updated_at=updated_at,
            connection=connection,
        )

    def bind_runtime_identity(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        native_session_id: str,
        protocol: str | None = None,
        protocol_version: str | None = None,
        native_version: str | None = None,
        compatibility_policy: str | None = None,
        updated_at: float,
        connection=None,
    ) -> bool:
        """Persist the exact native identity, guarded by the expected generation."""
        if connection is not None:
            return self._runtime_bindings.bind_identity(
                participant_id,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                protocol=protocol,
                protocol_version=protocol_version,
                native_version=native_version,
                compatibility_policy=compatibility_policy,
                updated_at=updated_at,
                connection=connection,
            )
        with self.write_unit() as unit:
            changed = self._runtime_bindings.bind_identity(
                participant_id,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                protocol=protocol,
                protocol_version=protocol_version,
                native_version=native_version,
                compatibility_policy=compatibility_policy,
                updated_at=updated_at,
                connection=unit.connection,
            )
            if changed:
                self._append_participant_controls_event(
                    unit, participant_id, recorded_at=updated_at
                )
            return changed

    def bind_runtime_and_participant_identity(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        native_session_id: str,
        session_correlation: str,
        protocol: str | None = None,
        protocol_version: str | None = None,
        native_version: str | None = None,
        compatibility_policy: str | None = None,
        updated_at: float,
    ) -> bool:
        """Atomically bind one native identity and its trusted participant identity.

        One fact in one write unit: a crash must never expose a new native route beside an
        older trusted transcript identity.
        """
        with self.write_unit() as unit:
            participant = self._participants.get(participant_id, connection=unit.connection)
            if participant is None:
                return False
            changed = self._runtime_bindings.bind_identity(
                participant_id,
                backend_generation=backend_generation,
                native_session_id=native_session_id,
                protocol=protocol,
                protocol_version=protocol_version,
                native_version=native_version,
                compatibility_policy=compatibility_policy,
                updated_at=updated_at,
                connection=unit.connection,
            )
            if not changed:
                return False
            participant.session_id = native_session_id
            participant.session_correlation = session_correlation
            self._participants.upsert(participant, connection=unit.connection)
            self._append_participant_controls_event(unit, participant_id, recorded_at=updated_at)
            return True

    def set_runtime_lifecycle(
        self,
        participant_id: str,
        phase,
        *,
        backend_generation: int,
        updated_at: float,
        connection=None,
    ) -> bool:
        """Advance one exact generation's lifecycle phase."""
        return self._runtime_bindings.set_lifecycle(
            participant_id,
            phase,
            backend_generation=backend_generation,
            updated_at=updated_at,
            connection=connection,
        )

    def delete_runtime_binding(self, participant_id: str, *, connection=None) -> None:
        if connection is not None:
            self._runtime_bindings.delete(participant_id, connection=connection)
            return
        with self.write_unit() as unit:
            before = self._runtime_bindings.get(participant_id, connection=unit.connection)
            self._runtime_bindings.delete(participant_id, connection=unit.connection)
            if before is not None:
                self._append_participant_controls_event(unit, participant_id, recorded_at=now())

    def _append_participant_controls_event(
        self, unit, participant_id: str, *, recorded_at: float
    ) -> None:
        participant = self._participants.get(participant_id, connection=unit.connection)
        if participant is None:
            return
        self.journal.append_group(
            unit,
            [
                participant_event(
                    self,
                    participant,
                    unit.connection,
                    revision=next_revision(self, unit.connection),
                    recorded_at=recorded_at,
                    kind="participant.controls_changed",
                )
            ],
        )

    def publish_participant_controls_changed(self, participant_id: str) -> None:
        """Journal one cached physical-control projection change."""
        with self.write_unit() as unit:
            self._append_participant_controls_event(unit, participant_id, recorded_at=now())
