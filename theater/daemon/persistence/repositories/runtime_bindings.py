"""Participant runtime bindings: persisted native wiring and recovery facts.

One row per participant is the daemon-owned record of how that participant is
wired natively: the selected wiring, backend generation, lifecycle phase,
private endpoint, verified process identity, exact native session identity,
and the executable/protocol compatibility facts needed to decide whether a
reconnect is safe. It never stores credentials.

Transaction boundaries (plan §2.3): launch intent is persisted before the
backend starts; exact identity is persisted before the initial dispatch.
Both boundaries are supported by the ``connection`` parameter — callers pass
one ``engine.begin()`` connection when a write must be atomic with adjacent
work (reserving a participant, writing a spawn job), and omit it for the
long-lived autocommit connection.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.harness import HARNESS_RUNTIME_LAUNCH_POLICY_MAX_BYTES
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories._runtime_validation import (
    bounded_id,
    generation,
    timestamp,
)
from theater.daemon.schema import participant_runtime_bindings
from theater.harness.contracts.runtime import (
    RuntimeBinding,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.harness.contracts.values import freeze_json_mapping

#: Lifecycle phases a restart must reconcile before assuming a backend is gone.
_RECOVERABLE_PHASES = (
    RuntimeLifecyclePhase.INTENDED,
    RuntimeLifecyclePhase.STARTED,
    RuntimeLifecyclePhase.BOUND,
    RuntimeLifecyclePhase.ATTACHED,
    RuntimeLifecyclePhase.ACTIVE,
    RuntimeLifecyclePhase.DETACHED,
)


@dataclass(frozen=True, slots=True)
class ParticipantRuntimeBinding:
    """The persisted form of one participant's native runtime binding."""

    participant_id: str
    harness: str
    wiring: RuntimeWiring
    backend_generation: int
    lifecycle: RuntimeLifecyclePhase
    endpoint: str | None = None
    backend_pid: int | None = None
    backend_started_at: float | None = None
    native_session_id: str | None = None
    protocol: str | None = None
    protocol_version: str | None = None
    native_version: str | None = None
    compatibility_policy: str | None = None
    launch_policy: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class RuntimeBindingRepository:
    """Reads and writes ``participant_runtime_bindings`` via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def upsert(
        self,
        binding: ParticipantRuntimeBinding,
        *,
        connection: Connection | None = None,
    ) -> None:
        """Idempotently persist one binding row, preserving creation time."""
        self._validate(binding)
        conn = self._db.conn if connection is None else connection
        stmt = sqlite_insert(participant_runtime_bindings).values(**self._values(binding))
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[participant_runtime_bindings.c.participant_id],
                set_={
                    "harness": binding.harness,
                    "wiring": str(binding.wiring),
                    "backend_generation": binding.backend_generation,
                    "lifecycle_phase": str(binding.lifecycle),
                    "endpoint": binding.endpoint,
                    "backend_pid": binding.backend_pid,
                    "backend_started_at": binding.backend_started_at,
                    "native_session_id": binding.native_session_id,
                    "protocol": binding.protocol,
                    "protocol_version": binding.protocol_version,
                    "native_version": binding.native_version,
                    "compatibility_policy": binding.compatibility_policy,
                    "launch_policy": binding.launch_policy,
                    "updated_at": binding.updated_at,
                },
            )
        )

    def record_launch_intent(
        self,
        binding: ParticipantRuntimeBinding,
        *,
        connection: Connection | None = None,
    ) -> None:
        """Persist launch intent before the backend starts.

        The row must exist with the exact wiring, backend generation, endpoint,
        and launch policy before any process is spawned, so a crash between
        reservation and backend start still leaves recoverable intent.
        """
        self.upsert(binding, connection=connection)

    def mark_backend_started(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        pid: int,
        started_at: float,
        connection: Connection | None = None,
    ) -> bool:
        """Record the verified process identity of a started backend.

        The update is guarded by the expected ``backend_generation`` and never
        assigns the generation itself: a delayed callback from a superseded
        generation must not overwrite the current generation's process
        identity. Returns whether the expected generation's row was updated;
        callers fail closed on ``False``.
        """
        conn = self._db.conn if connection is None else connection
        result = conn.execute(
            participant_runtime_bindings.update()
            .where(participant_runtime_bindings.c.participant_id == participant_id)
            .where(participant_runtime_bindings.c.backend_generation == backend_generation)
            .values(
                backend_pid=pid,
                backend_started_at=started_at,
                lifecycle_phase=str(RuntimeLifecyclePhase.STARTED),
                updated_at=started_at,
            )
        )
        return bool(result.rowcount)

    def bind_identity(
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
        connection: Connection | None = None,
    ) -> bool:
        """Persist the exact native session identity before initial dispatch.

        Identity binds to the participant, its backend generation, and the
        verified private backend — never the working directory alone. The
        update is guarded by the expected ``backend_generation`` and never
        assigns the generation itself: a delayed callback from a superseded
        generation must not overwrite the current generation's identity.
        Returns whether the expected generation's row was updated; callers
        fail closed on ``False``.
        """
        conn = self._db.conn if connection is None else connection
        result = conn.execute(
            participant_runtime_bindings.update()
            .where(participant_runtime_bindings.c.participant_id == participant_id)
            .where(participant_runtime_bindings.c.backend_generation == backend_generation)
            .values(
                native_session_id=native_session_id,
                protocol=protocol,
                protocol_version=protocol_version,
                native_version=native_version,
                compatibility_policy=compatibility_policy,
                lifecycle_phase=str(RuntimeLifecyclePhase.BOUND),
                updated_at=updated_at,
            )
        )
        return bool(result.rowcount)

    def set_lifecycle(
        self,
        participant_id: str,
        phase: RuntimeLifecyclePhase,
        *,
        backend_generation: int,
        updated_at: float,
        connection: Connection | None = None,
    ) -> bool:
        """Advance one exact generation's lifecycle phase.

        Guarded by the expected ``backend_generation`` so a stale callback
        cannot move the current generation's phase. Returns whether the
        expected generation's row was updated; callers fail closed on
        ``False``.
        """
        conn = self._db.conn if connection is None else connection
        result = conn.execute(
            participant_runtime_bindings.update()
            .where(participant_runtime_bindings.c.participant_id == participant_id)
            .where(participant_runtime_bindings.c.backend_generation == backend_generation)
            .values(lifecycle_phase=str(phase), updated_at=updated_at)
        )
        return bool(result.rowcount)

    def get(self, participant_id: str) -> ParticipantRuntimeBinding | None:
        row = self._db.conn.execute(
            select(participant_runtime_bindings).where(
                participant_runtime_bindings.c.participant_id == participant_id
            )
        ).first()
        return self._from_row(dict(row._mapping)) if row else None

    def find_by_native_session(self, native_session_id: str) -> ParticipantRuntimeBinding | None:
        """Exact identity lookup; the only non-heuristic session search."""
        row = self._db.conn.execute(
            select(participant_runtime_bindings).where(
                participant_runtime_bindings.c.native_session_id == native_session_id
            )
        ).first()
        return self._from_row(dict(row._mapping)) if row else None

    def list_recoverable(self) -> list[ParticipantRuntimeBinding]:
        """Bindings a daemon restart must reconcile before assuming loss."""
        rows = self._db.conn.execute(
            select(participant_runtime_bindings).where(
                participant_runtime_bindings.c.lifecycle_phase.in_(
                    [str(phase) for phase in _RECOVERABLE_PHASES]
                )
            )
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def delete(self, participant_id: str, *, connection: Connection | None = None) -> None:
        conn = self._db.conn if connection is None else connection
        conn.execute(
            participant_runtime_bindings.delete().where(
                participant_runtime_bindings.c.participant_id == participant_id
            )
        )

    def _validate(self, binding: ParticipantRuntimeBinding) -> None:
        """Reject values that would bypass the public contract's bounds.

        Persistence reuses the public ``RuntimeBinding`` contract exactly: its
        constructor bounds identifiers, policy names, endpoint length, and the
        JSON compatibility of launch-policy values, so nothing can enter the
        table that the public contract would reject. Harness and timestamp
        fields — which the public contract does not carry — are validated here
        the same way: rejected, never truncated.
        """
        bounded_id(binding.participant_id, "binding participant_id")
        bounded_id(binding.harness, "binding harness")
        generation(binding.backend_generation, "binding backend_generation")
        timestamp(binding.created_at, "binding created_at")
        timestamp(binding.updated_at, "binding updated_at")
        launch_policy: Mapping[str, object] = {}
        if binding.launch_policy is not None:
            try:
                decoded = json.loads(binding.launch_policy)
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("launch policy must be a JSON object") from exc
            if not isinstance(decoded, dict):
                raise ValueError("launch policy must be a JSON object")
            launch_policy = decoded
        RuntimeBinding(
            participant_id=binding.participant_id,
            backend_generation=binding.backend_generation,
            wiring=binding.wiring,
            lifecycle=binding.lifecycle,
            endpoint=binding.endpoint,
            pid=binding.backend_pid,
            native_session_id=binding.native_session_id,
            protocol=binding.protocol,
            protocol_version=binding.protocol_version,
            native_version=binding.native_version,
            compatibility_policy=binding.compatibility_policy,
            launch_policy=launch_policy,
        )

    def _values(self, binding: ParticipantRuntimeBinding) -> Mapping[str, Any]:
        return {
            "participant_id": binding.participant_id,
            "harness": binding.harness,
            "wiring": str(binding.wiring),
            "backend_generation": binding.backend_generation,
            "lifecycle_phase": str(binding.lifecycle),
            "endpoint": binding.endpoint,
            "backend_pid": binding.backend_pid,
            "backend_started_at": binding.backend_started_at,
            "native_session_id": binding.native_session_id,
            "protocol": binding.protocol,
            "protocol_version": binding.protocol_version,
            "native_version": binding.native_version,
            "compatibility_policy": binding.compatibility_policy,
            "launch_policy": binding.launch_policy,
            "created_at": binding.created_at,
            "updated_at": binding.updated_at,
        }

    def _from_row(self, row: Mapping[str, Any]) -> ParticipantRuntimeBinding:
        return ParticipantRuntimeBinding(
            participant_id=row["participant_id"],
            harness=row["harness"],
            wiring=RuntimeWiring(row["wiring"]),
            backend_generation=row["backend_generation"],
            lifecycle=RuntimeLifecyclePhase(row["lifecycle_phase"]),
            endpoint=row["endpoint"],
            backend_pid=row["backend_pid"],
            backend_started_at=row["backend_started_at"],
            native_session_id=row["native_session_id"],
            protocol=row["protocol"],
            protocol_version=row["protocol_version"],
            native_version=row["native_version"],
            compatibility_policy=row["compatibility_policy"],
            launch_policy=row["launch_policy"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


def encode_launch_policy(launch_policy: Mapping[str, object]) -> str:
    """Encode bounded, credential-free launch-policy facts as JSON.

    Values are validated JSON-compatible and finite (the public contract's
    freeze rules) before encoding, and the encoded form must fit
    ``HARNESS_RUNTIME_LAUNCH_POLICY_MAX_BYTES`` UTF-8 bytes. Malformed or
    oversized policy is rejected, never truncated.
    """
    freeze_json_mapping(launch_policy)
    encoded = json.dumps(launch_policy, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > HARNESS_RUNTIME_LAUNCH_POLICY_MAX_BYTES:
        raise ValueError(
            "launch policy exceeds "
            f"{HARNESS_RUNTIME_LAUNCH_POLICY_MAX_BYTES} UTF-8 bytes when encoded"
        )
    return encoded


__all__ = [
    "ParticipantRuntimeBinding",
    "RuntimeBindingRepository",
    "encode_launch_policy",
]
