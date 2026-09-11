"""Control operations: durable reservation, delivery phase, and queue facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, and_, case, delete, exists, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import (
    CONTROL_OPERATION_PAYLOAD_MAX_BYTES,
    RUNTIME_STORAGE_PRUNE_BATCH,
)
from theater.constants.harness import HARNESS_RUNTIME_ERROR_MAX_CHARS
from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories._runtime_validation import (
    bounded_id,
    optional_bounded_id,
    optional_bounded_text,
    optional_generation,
    optional_queue_sequence,
    timestamp,
)
from theater.daemon.schema import control_operations, jobs
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    DeliveryResult,
)
from theater.models import Job


class ControlOperationAmbiguityError(Exception):
    """More than one job-bearing operation matches one exact native turn."""


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
    #: A prompt crossed native transmission without an authoritative delivery outcome.
    execution_barrier: bool = False
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
        """Persist one operation before transmission; the id is its identity."""
        self._validate(operation)
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
        execution_barrier: bool | None = None,
        updated_at: float,
        connection: Connection | None = None,
    ) -> None:
        """Persist that transmission is starting; ack may never arrive."""
        conn = self._db.conn if connection is None else connection
        values: dict[str, Any] = {
            "delivery_phase": str(ControlDeliveryPhase.DISPATCHED),
            "native_session_id": native_session_id,
            "native_turn_id": native_turn_id,
            "updated_at": updated_at,
        }
        if execution_barrier is not None:
            values["execution_barrier"] = int(execution_barrier)
        else:
            # Native prompt dispatch sets a barrier until an explicit receipt/evidence/idle
            # transition.
            values["execution_barrier"] = case(
                (
                    and_(
                        control_operations.c.transport == str(ControlTransport.NATIVE_RUNTIME),
                        control_operations.c.kind.in_(
                            [str(ControlKind.SEND), str(ControlKind.QUEUE_FOLLOWUP)]
                        ),
                    ),
                    1,
                ),
                else_=control_operations.c.execution_barrier,
            )
        conn.execute(
            control_operations.update()
            .where(control_operations.c.operation_id == operation_id)
            .values(**values)
        )

    def settle(
        self,
        operation_id: str,
        *,
        result: DeliveryResult,
        native_turn_id: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        execution_barrier: bool | None = None,
        updated_at: float,
        connection: Connection | None = None,
    ) -> None:
        """Record a terminal delivery result; job state is separate metadata."""
        conn = self._db.conn if connection is None else connection
        values: dict[str, Any] = {
            "delivery_phase": str(ControlDeliveryPhase.SETTLED),
            "delivery_result": str(result),
            "native_turn_id": native_turn_id,
            "error_code": error_code,
            "error": error,
            "updated_at": updated_at,
        }
        if execution_barrier is not None:
            values["execution_barrier"] = int(execution_barrier)
        elif result in (DeliveryResult.ACCEPTED, DeliveryResult.REJECTED):
            # A definitive receipt closes the execution boundary.  Unknown
            # receipt handling below deliberately keeps/reasserts it.
            values["execution_barrier"] = 0
        else:
            values["execution_barrier"] = case(
                (
                    and_(
                        control_operations.c.transport == str(ControlTransport.NATIVE_RUNTIME),
                        control_operations.c.kind.in_(
                            [str(ControlKind.SEND), str(ControlKind.QUEUE_FOLLOWUP)]
                        ),
                    ),
                    1,
                ),
                else_=control_operations.c.execution_barrier,
            )
        conn.execute(
            control_operations.update()
            .where(control_operations.c.operation_id == operation_id)
            .values(**values)
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

    def set_queued_payload(
        self, operation_id: str, payload: str, *, connection: Connection | None = None
    ) -> None:
        """Update bounded causal metadata without binding an undelivered job."""
        optional_bounded_text(
            payload, "operation payload", limit=CONTROL_OPERATION_PAYLOAD_MAX_BYTES
        )
        if len(payload.encode("utf-8")) > CONTROL_OPERATION_PAYLOAD_MAX_BYTES:
            raise ValueError("operation payload exceeds the bounded byte limit")
        conn = self._db.conn if connection is None else connection
        conn.execute(
            control_operations.update()
            .where(control_operations.c.operation_id == operation_id)
            .where(control_operations.c.delivery_phase == str(ControlDeliveryPhase.QUEUED))
            .values(payload=payload)
        )

    def dispatched_for_participant(self, participant_id: str) -> list[ControlOperation]:
        """Operations whose transmission began and whose ack may never arrive."""
        rows = self._db.conn.execute(
            select(control_operations)
            .where(control_operations.c.participant_id == participant_id)
            .where(control_operations.c.delivery_phase == str(ControlDeliveryPhase.DISPATCHED))
            .order_by(control_operations.c.created_at.asc())
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def execution_barriers_for_participant(self, participant_id: str) -> list[ControlOperation]:
        """Unresolved native prompt executions, in durable creation order."""
        rows = self._db.conn.execute(
            select(control_operations)
            .where(control_operations.c.participant_id == participant_id)
            .where(control_operations.c.execution_barrier == 1)
            .where(control_operations.c.transport == str(ControlTransport.NATIVE_RUNTIME))
            .where(
                control_operations.c.kind.in_(
                    [str(ControlKind.SEND), str(ControlKind.QUEUE_FOLLOWUP)]
                )
            )
            .order_by(
                control_operations.c.created_at.asc(),
                control_operations.c.operation_id.asc(),
            )
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def has_execution_barrier(self, participant_id: str) -> bool:
        """Whether any unresolved native prompt blocks automated delivery."""
        return bool(
            self._db.conn.execute(
                select(
                    exists()
                    .where(control_operations.c.participant_id == participant_id)
                    .where(control_operations.c.execution_barrier == 1)
                    .where(control_operations.c.transport == str(ControlTransport.NATIVE_RUNTIME))
                    .where(
                        control_operations.c.kind.in_(
                            [str(ControlKind.SEND), str(ControlKind.QUEUE_FOLLOWUP)]
                        )
                    )
                )
            ).scalar_one()
        )

    def unresolved_prompt_deliveries_for_participant(
        self, participant_id: str
    ) -> list[ControlOperation]:
        """Prompt deliveries still waiting for evidence or their deadline."""
        rows = self._db.conn.execute(
            select(control_operations)
            .where(control_operations.c.participant_id == participant_id)
            .where(control_operations.c.transport == str(ControlTransport.NATIVE_RUNTIME))
            .where(
                control_operations.c.kind.in_(
                    [str(ControlKind.SEND), str(ControlKind.QUEUE_FOLLOWUP)]
                )
            )
            .where(control_operations.c.job_handle.isnot(None))
            .where(
                exists()
                .where(jobs.c.handle == control_operations.c.job_handle)
                .where(jobs.c.state == "running")
            )
            .where(
                or_(
                    control_operations.c.delivery_phase == str(ControlDeliveryPhase.DISPATCHED),
                    and_(
                        control_operations.c.delivery_phase == str(ControlDeliveryPhase.SETTLED),
                        control_operations.c.delivery_result == str(DeliveryResult.UNKNOWN),
                    ),
                )
            )
            .order_by(
                control_operations.c.created_at.asc(),
                control_operations.c.operation_id.asc(),
            )
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def set_execution_barrier(
        self,
        operation_id: str,
        *,
        active: bool,
        updated_at: float,
        connection: Connection | None = None,
    ) -> None:
        """Set one prompt's durable unresolved-execution barrier."""
        conn = self._db.conn if connection is None else connection
        conn.execute(
            control_operations.update()
            .where(control_operations.c.operation_id == operation_id)
            .values(execution_barrier=int(active), updated_at=updated_at)
        )

    def active_running_for_target(self, target_id: str) -> list[Job]:
        """Running jobs actually delivered to the backend, oldest first."""
        native_prompt = and_(
            control_operations.c.transport == str(ControlTransport.NATIVE_RUNTIME),
            control_operations.c.kind.in_([str(ControlKind.SEND), str(ControlKind.QUEUE_FOLLOWUP)]),
        )
        # A prompt with UNKNOWN delivery stays a running job until evidence or deadline resolution.
        unresolved_execution = or_(
            ~native_prompt,
            control_operations.c.execution_barrier == 1,
        )
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
                    and_(
                        control_operations.c.delivery_phase == str(ControlDeliveryPhase.DISPATCHED),
                        unresolved_execution,
                    ),
                    and_(
                        control_operations.c.delivery_phase == str(ControlDeliveryPhase.SETTLED),
                        or_(
                            control_operations.c.delivery_result == str(DeliveryResult.ACCEPTED),
                            and_(
                                control_operations.c.delivery_result == str(DeliveryResult.UNKNOWN),
                                unresolved_execution,
                            ),
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

    def for_native_turn(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
    ) -> ControlOperation | None:
        """The unique job-bearing operation correlated to one exact native turn."""
        rows = self._db.conn.execute(
            select(control_operations)
            .where(control_operations.c.participant_id == participant_id)
            .where(control_operations.c.backend_generation == backend_generation)
            .where(control_operations.c.native_session_id == native_session_id)
            .where(control_operations.c.native_turn_id == native_turn_id)
            .where(control_operations.c.job_handle.isnot(None))
            .where(
                control_operations.c.kind.in_(
                    [str(ControlKind.SEND), str(ControlKind.QUEUE_FOLLOWUP)]
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
            .limit(2)
        ).fetchall()
        if not rows:
            return None
        if len(rows) > 1:
            raise ControlOperationAmbiguityError(
                "multiple job-bearing operations match native turn "
                f"{participant_id}/{backend_generation}/{native_session_id}/{native_turn_id}"
            )
        return self._from_row(dict(rows[0]._mapping))

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

    def in_phases(
        self, participant_id: str, phases: Sequence[ControlDeliveryPhase]
    ) -> list[ControlOperation]:
        """Every operation still in the given phases — job-bearing and jobless."""
        rows = self._db.conn.execute(
            select(control_operations)
            .where(control_operations.c.participant_id == participant_id)
            .where(control_operations.c.delivery_phase.in_([str(phase) for phase in phases]))
            .order_by(
                control_operations.c.created_at.asc(),
                control_operations.c.operation_id.asc(),
            )
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def prune(
        self,
        *,
        older_than: float,
        limit: int = RUNTIME_STORAGE_PRUNE_BATCH,
        connection: Connection | None = None,
    ) -> int:
        """Delete settled operations older than a cutoff, bounded by ``limit``."""
        if limit <= 0:
            return 0
        conn = self._db.conn if connection is None else connection
        stale = (
            select(control_operations.c.operation_id)
            .where(control_operations.c.delivery_phase == str(ControlDeliveryPhase.SETTLED))
            .where(control_operations.c.execution_barrier == 0)
            .where(control_operations.c.updated_at < older_than)
            .where(
                or_(
                    control_operations.c.job_handle.is_(None),
                    ~control_operations.c.kind.in_(
                        [str(ControlKind.SEND), str(ControlKind.QUEUE_FOLLOWUP)]
                    ),
                    ~exists()
                    .where(jobs.c.handle == control_operations.c.job_handle)
                    .where(jobs.c.state == "running"),
                )
            )
            .order_by(control_operations.c.updated_at.asc())
            .limit(limit)
        )
        result = conn.execute(
            delete(control_operations).where(control_operations.c.operation_id.in_(stale))
        )
        return int(result.rowcount or 0)

    def _validate(self, operation: ControlOperation) -> None:
        """Reject malformed or oversized rows before persistence."""
        bounded_id(operation.operation_id, "operation operation_id")
        bounded_id(operation.participant_id, "operation participant_id")
        optional_bounded_id(operation.job_handle, "operation job_handle")
        if not isinstance(operation.kind, ControlKind):
            raise TypeError("operation kind must be a ControlKind")
        if not isinstance(operation.transport, ControlTransport):
            raise TypeError("operation transport must be a ControlTransport")
        if not isinstance(operation.delivery_phase, ControlDeliveryPhase):
            raise TypeError("operation delivery_phase must be a ControlDeliveryPhase")
        if operation.delivery_result is not None and not isinstance(
            operation.delivery_result, DeliveryResult
        ):
            raise TypeError("operation delivery_result must be a DeliveryResult or null")
        if not isinstance(operation.execution_barrier, bool):
            raise TypeError("operation execution_barrier must be a bool")
        optional_generation(operation.backend_generation, "operation backend_generation")
        optional_bounded_id(operation.native_session_id, "operation native_session_id")
        optional_bounded_id(operation.native_turn_id, "operation native_turn_id")
        optional_queue_sequence(operation.queue_sequence, "operation queue_sequence")
        optional_bounded_id(operation.error_code, "operation error_code")
        optional_bounded_text(
            operation.error, "operation error", limit=HARNESS_RUNTIME_ERROR_MAX_CHARS
        )
        timestamp(operation.created_at, "operation created_at")
        timestamp(operation.updated_at, "operation updated_at")
        if operation.payload is not None:
            payload_bytes = len(operation.payload.encode("utf-8"))
            if payload_bytes > CONTROL_OPERATION_PAYLOAD_MAX_BYTES:
                raise ValueError(
                    "control operation payload exceeds "
                    f"{CONTROL_OPERATION_PAYLOAD_MAX_BYTES} UTF-8 bytes"
                )

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
            "execution_barrier": int(operation.execution_barrier),
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
            execution_barrier=bool(row["execution_barrier"]),
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


__all__ = [
    "ControlOperation",
    "ControlOperationAmbiguityError",
    "ControlOperationRepository",
]
