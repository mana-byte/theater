"""Control operations: durable reservation, delivery phase, and queue facts.

One row per durably reserved control. The reservation exists before
transmission; ``DISPATCHED`` is persisted before the write reaches the wire so
an interrupted transmission stays potentially delivered; ``SETTLED`` carries a
terminal delivery result. Job state stays running/done/crashed/killed and is
never implied by a delivery phase.

Queue position comes from the persisted send-sequence allocator (the ``meta``
table), never ``MAX(...)``, timestamps, or an in-memory counter; the counter
survives pruned operation rows.

Seams for the audit's queued-job theft bug: ``dispatched_for_participant``
and ``active_running_for_target`` answer "what actually reached the backend"
(dispatched, or settled accepted/unknown), while the job repository's
all-running queries remain untouched for cancellation and lifecycle handling.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, and_, delete, exists, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import (
    CONTROL_OPERATION_PAYLOAD_MAX_BYTES,
    RUNTIME_STORAGE_PRUNE_BATCH,
)
from theater.daemon.persistence.database import Database
from theater.daemon.schema import control_operations, jobs
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import Job


@dataclass(frozen=True, slots=True)
class ControlOperation:
    """The persisted form of one durably reserved control."""

    operation_id: str
    participant_id: str
    kind: ControlKind
    transport: ControlTransport
    delivery_phase: ControlDeliveryPhase
    job_handle: str | None = None
    delivery_result: DeliveryResult | None = None
    backend_generation: int | None = None
    native_session_id: str | None = None
    native_turn_id: str | None = None
    queue_sequence: int | None = None
    payload: str | None = None
    error_code: str | None = None
    error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0


class ControlOperationRepository:
    """Reads and writes ``control_operations`` via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def reserve(
        self,
        operation: ControlOperation,
        *,
        connection: Connection | None = None,
    ) -> None:
        """Persist one operation before transmission; the id is its identity.

        A persisted operation id never justifies retrying a native mutation —
        it exists so an interrupted delivery can be found and reconciled.
        The payload is rejected when its UTF-8 encoding exceeds
        ``CONTROL_OPERATION_PAYLOAD_MAX_BYTES`` bytes; identity and evidence
        are never truncated to fit.
        """
        if operation.payload is not None:
            payload_bytes = len(operation.payload.encode("utf-8"))
            if payload_bytes > CONTROL_OPERATION_PAYLOAD_MAX_BYTES:
                raise ValueError(
                    "control operation payload exceeds "
                    f"{CONTROL_OPERATION_PAYLOAD_MAX_BYTES} UTF-8 bytes"
                )
        conn = self._db.conn if connection is None else connection
        conn.execute(
            sqlite_insert(control_operations)
            .values(**self._values(operation))
            .on_conflict_do_nothing(index_elements=[control_operations.c.operation_id])
        )

    def mark_dispatched(
        self,
        operation_id: str,
        *,
        native_session_id: str | None = None,
        native_turn_id: str | None = None,
        updated_at: float,
        connection: Connection | None = None,
    ) -> None:
        """Persist that transmission is starting; ack may never arrive."""
        conn = self._db.conn if connection is None else connection
        conn.execute(
            control_operations.update()
            .where(control_operations.c.operation_id == operation_id)
            .values(
                delivery_phase=str(ControlDeliveryPhase.DISPATCHED),
                native_session_id=native_session_id,
                native_turn_id=native_turn_id,
                updated_at=updated_at,
            )
        )

    def settle(
        self,
        operation_id: str,
        *,
        result: DeliveryResult,
        native_turn_id: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        updated_at: float,
        connection: Connection | None = None,
    ) -> None:
        """Record a terminal delivery result; job state is separate metadata."""
        conn = self._db.conn if connection is None else connection
        conn.execute(
            control_operations.update()
            .where(control_operations.c.operation_id == operation_id)
            .values(
                delivery_phase=str(ControlDeliveryPhase.SETTLED),
                delivery_result=str(result),
                native_turn_id=native_turn_id,
                error_code=error_code,
                error=error,
                updated_at=updated_at,
            )
        )

    def get(self, operation_id: str) -> ControlOperation | None:
        row = self._db.conn.execute(
            select(control_operations).where(control_operations.c.operation_id == operation_id)
        ).first()
        return self._from_row(dict(row._mapping)) if row else None

    def for_job(self, job_handle: str) -> list[ControlOperation]:
        rows = self._db.conn.execute(
            select(control_operations)
            .where(control_operations.c.job_handle == job_handle)
            .order_by(control_operations.c.created_at.asc())
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def queued_for_participant(self, participant_id: str) -> list[ControlOperation]:
        """Queued followups in FIFO order by their allocated send sequence."""
        rows = self._db.conn.execute(
            select(control_operations)
            .where(control_operations.c.participant_id == participant_id)
            .where(control_operations.c.delivery_phase == str(ControlDeliveryPhase.QUEUED))
            .order_by(control_operations.c.queue_sequence.asc())
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def dispatched_for_participant(self, participant_id: str) -> list[ControlOperation]:
        """Operations whose transmission began and whose ack may never arrive."""
        rows = self._db.conn.execute(
            select(control_operations)
            .where(control_operations.c.participant_id == participant_id)
            .where(control_operations.c.delivery_phase == str(ControlDeliveryPhase.DISPATCHED))
            .order_by(control_operations.c.created_at.asc())
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def active_running_for_target(self, target_id: str) -> list[Job]:
        """Running jobs actually delivered to the backend, oldest first.

        A job is active when it is native and its transmission began — the
        operation reached ``DISPATCHED``, or it ``SETTLED`` with
        ``ACCEPTED``/``UNKNOWN`` delivery (an accepted or possibly-delivered
        turn keeps the job running until terminal evidence completes it) —
        or when it is legacy and has no control operation at all. ``RESERVED``
        and ``QUEUED`` operations, and ``SETTLED``/``REJECTED`` operations,
        never make a job active, so a queued followup can never become the
        oldest eligible active job by accident.

        Both predicates are correlated ``EXISTS`` checks scoped to the
        job's target participant: operations with a NULL ``job_handle``
        (settings/interrupt follow no Theater job) match no job, and another
        participant's operations never reclassify this one. The job
        repository's all-running queries stay untouched for cancellation and
        lifecycle handling.
        """
        native_active = (
            exists()
            .where(control_operations.c.job_handle == jobs.c.handle)
            .where(control_operations.c.participant_id == jobs.c.target_id)
            .where(
                control_operations.c.kind.in_(
                    [
                        str(ControlKind.SEND),
                        str(ControlKind.QUEUE_FOLLOWUP),
                        str(ControlKind.STEER),
                    ]
                )
            )
            .where(
                or_(
                    control_operations.c.delivery_phase == str(ControlDeliveryPhase.DISPATCHED),
                    and_(
                        control_operations.c.delivery_phase == str(ControlDeliveryPhase.SETTLED),
                        control_operations.c.delivery_result.in_(
                            [str(DeliveryResult.ACCEPTED), str(DeliveryResult.UNKNOWN)]
                        ),
                    ),
                )
            )
        )
        has_operation = (
            exists()
            .where(control_operations.c.job_handle == jobs.c.handle)
            .where(control_operations.c.job_handle.isnot(None))
            .where(control_operations.c.participant_id == jobs.c.target_id)
        )
        rows = self._db.conn.execute(
            select(jobs)
            .where(jobs.c.target_id == target_id)
            .where(jobs.c.state == "running")
            .where(or_(native_active, ~has_operation))
            .order_by(jobs.c.created_at.asc())
        ).fetchall()
        return [Job.from_row(row._mapping) for row in rows]

    def pending_count_for_participant(self, participant_id: str) -> int:
        """How many queued followups one participant holds right now."""
        return int(
            self._db.conn.execute(
                select(func.count())
                .select_from(control_operations)
                .where(control_operations.c.participant_id == participant_id)
                .where(control_operations.c.delivery_phase == str(ControlDeliveryPhase.QUEUED))
            ).scalar_one()
        )

    def prune(
        self,
        *,
        older_than: float,
        limit: int = RUNTIME_STORAGE_PRUNE_BATCH,
        connection: Connection | None = None,
    ) -> int:
        """Delete settled operations older than a cutoff, bounded by ``limit``.

        Only the GC service may call this, and only once the operation's
        recovery and retention obligations have ended. Never prunes queued or
        dispatched rows.
        """
        if limit <= 0:
            return 0
        conn = self._db.conn if connection is None else connection
        stale = (
            select(control_operations.c.operation_id)
            .where(control_operations.c.delivery_phase == str(ControlDeliveryPhase.SETTLED))
            .where(control_operations.c.updated_at < older_than)
            .order_by(control_operations.c.updated_at.asc())
            .limit(limit)
        )
        result = conn.execute(
            delete(control_operations).where(control_operations.c.operation_id.in_(stale))
        )
        return int(result.rowcount or 0)

    def _values(self, operation: ControlOperation) -> Mapping[str, Any]:
        return {
            "operation_id": operation.operation_id,
            "participant_id": operation.participant_id,
            "job_handle": operation.job_handle,
            "kind": str(operation.kind),
            "transport": str(operation.transport),
            "delivery_phase": str(operation.delivery_phase),
            "delivery_result": (
                None if operation.delivery_result is None else str(operation.delivery_result)
            ),
            "backend_generation": operation.backend_generation,
            "native_session_id": operation.native_session_id,
            "native_turn_id": operation.native_turn_id,
            "queue_sequence": operation.queue_sequence,
            "payload": operation.payload,
            "error_code": operation.error_code,
            "error": operation.error,
            "created_at": operation.created_at,
            "updated_at": operation.updated_at,
        }

    def _from_row(self, row: Mapping[str, Any]) -> ControlOperation:
        return ControlOperation(
            operation_id=row["operation_id"],
            participant_id=row["participant_id"],
            job_handle=row["job_handle"],
            kind=ControlKind(row["kind"]),
            transport=ControlTransport(row["transport"]),
            delivery_phase=ControlDeliveryPhase(row["delivery_phase"]),
            delivery_result=None
            if row["delivery_result"] is None
            else DeliveryResult(row["delivery_result"]),
            backend_generation=row["backend_generation"],
            native_session_id=row["native_session_id"],
            native_turn_id=row["native_turn_id"],
            queue_sequence=row["queue_sequence"],
            payload=row["payload"],
            error_code=row["error_code"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


__all__ = ["ControlOperation", "ControlOperationRepository"]
