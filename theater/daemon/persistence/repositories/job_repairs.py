"""Internal repair attempts of strict jobs, their safety fence, and the contract audit."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import Connection, exists, insert, select, update
from sqlalchemy.engine import ScalarResult

from theater.constants.response_contract import CONTRACT_AUDIT_DETAIL_MAX_CHARS
from theater.daemon.persistence.database import Database
from theater.daemon.schema import job_contract_audit, job_repairs


class RepairState(StrEnum):
    """``pending`` never reached the wire; ``dispatched`` may have; ``settled`` is closed."""

    PENDING = "pending"
    DISPATCHED = "dispatched"
    AWAITING = "awaiting"
    UNKNOWN = "unknown"
    SETTLED = "settled"


_KEEP = object()


@dataclass(frozen=True, slots=True)
class RepairAttempt:
    """One repair prompt for one public job; the fence outlives idle but not positive evidence."""

    attempt_id: str
    job_handle: str
    participant_id: str
    attempt: int
    state: RepairState
    fence: bool = True
    backend_generation: int | None = None
    native_session_id: str | None = None
    source_turn_id: str | None = None
    native_turn_id: str | None = None
    turn_proven: bool = False
    reason: str | None = None
    outcome: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.state is not RepairState.SETTLED


def _from_row(row) -> RepairAttempt:
    values = dict(row._mapping)
    values["state"] = RepairState(values["state"])
    values["fence"] = bool(values["fence"])
    values["turn_proven"] = bool(values["turn_proven"])
    return RepairAttempt(**values)


class JobRepairRepository:
    """Reads and writes ``job_repairs`` and ``job_contract_audit`` via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def create(self, attempt: RepairAttempt, *, connection: Connection | None = None) -> bool:
        """Insert one attempt; ``False`` when that (job, attempt) already exists."""
        conn = self._db.conn if connection is None else connection
        taken = conn.execute(
            select(
                exists()
                .where(job_repairs.c.job_handle == attempt.job_handle)
                .where(job_repairs.c.attempt == attempt.attempt)
            )
        ).scalar_one()
        if taken:
            return False
        conn.execute(
            insert(job_repairs).values(
                attempt_id=attempt.attempt_id,
                job_handle=attempt.job_handle,
                participant_id=attempt.participant_id,
                attempt=attempt.attempt,
                state=str(attempt.state),
                fence=int(attempt.fence),
                backend_generation=attempt.backend_generation,
                native_session_id=attempt.native_session_id,
                source_turn_id=attempt.source_turn_id,
                native_turn_id=attempt.native_turn_id,
                turn_proven=int(attempt.turn_proven),
                reason=attempt.reason,
                outcome=attempt.outcome,
                created_at=attempt.created_at,
                updated_at=attempt.updated_at,
            )
        )
        return True

    def get(self, attempt_id: str, *, connection: Connection | None = None) -> RepairAttempt | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(job_repairs).where(job_repairs.c.attempt_id == attempt_id)
        ).first()
        return _from_row(row) if row else None

    def for_job(
        self, job_handle: str, *, connection: Connection | None = None
    ) -> list[RepairAttempt]:
        conn = self._db.conn if connection is None else connection
        rows = conn.execute(
            select(job_repairs)
            .where(job_repairs.c.job_handle == job_handle)
            .order_by(job_repairs.c.attempt.asc())
        ).fetchall()
        return [_from_row(row) for row in rows]

    def open_for_participant(
        self, participant_id: str, *, connection: Connection | None = None
    ) -> list[RepairAttempt]:
        """Unsettled attempts in creation order, including fenced ``unknown`` ones."""
        conn = self._db.conn if connection is None else connection
        rows = conn.execute(
            select(job_repairs)
            .where(job_repairs.c.participant_id == participant_id)
            .where(job_repairs.c.state != str(RepairState.SETTLED))
            .order_by(job_repairs.c.created_at.asc(), job_repairs.c.attempt_id.asc())
        ).fetchall()
        return [_from_row(row) for row in rows]

    def fence_active(self, participant_id: str, *, connection: Connection | None = None) -> bool:
        conn = self._db.conn if connection is None else connection
        return bool(
            conn.execute(
                select(
                    exists()
                    .where(job_repairs.c.participant_id == participant_id)
                    .where(job_repairs.c.fence == 1)
                )
            ).scalar_one()
        )

    def for_native_turn(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
        connection: Connection | None = None,
    ) -> RepairAttempt | None:
        """The attempt whose repair prompt started this exact turn, settled or not."""
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(job_repairs)
            .where(job_repairs.c.participant_id == participant_id)
            .where(job_repairs.c.backend_generation == backend_generation)
            .where(job_repairs.c.native_session_id == native_session_id)
            .where(job_repairs.c.native_turn_id == native_turn_id)
            .order_by(job_repairs.c.created_at.asc())
            .limit(1)
        ).first()
        return _from_row(row) if row else None

    def repairing(
        self, handles: Sequence[str], *, connection: Connection | None = None
    ) -> set[str]:
        """Which of these jobs still have an unsettled repair attempt."""
        if not handles:
            return set()
        conn = self._db.conn if connection is None else connection
        unsettled: ScalarResult[str] = conn.execute(
            select(job_repairs.c.job_handle)
            .where(job_repairs.c.job_handle.in_(list(handles)))
            .where(job_repairs.c.state != str(RepairState.SETTLED))
        ).scalars()
        return {str(handle) for handle in unsettled}

    def update(
        self,
        attempt_id: str,
        *,
        updated_at: float,
        state: RepairState | object = _KEEP,
        fence: bool | object = _KEEP,
        native_turn_id: str | object | None = _KEEP,
        turn_proven: bool | object = _KEEP,
        outcome: str | object | None = _KEEP,
        connection: Connection | None = None,
    ) -> None:
        values: dict[str, object] = {"updated_at": updated_at}
        if state is not _KEEP:
            values["state"] = str(state)
        if fence is not _KEEP:
            values["fence"] = int(bool(fence))
        if native_turn_id is not _KEEP:
            values["native_turn_id"] = native_turn_id
        if turn_proven is not _KEEP:
            values["turn_proven"] = int(bool(turn_proven))
        if outcome is not _KEEP:
            values["outcome"] = outcome
        conn = self._db.conn if connection is None else connection
        conn.execute(
            update(job_repairs).where(job_repairs.c.attempt_id == attempt_id).values(**values)
        )

    def add_audit(
        self,
        job_handle: str,
        participant_id: str | None,
        event: str,
        detail: str | None,
        *,
        at: float,
        connection: Connection | None = None,
    ) -> None:
        conn = self._db.conn if connection is None else connection
        conn.execute(
            insert(job_contract_audit).values(
                job_handle=job_handle,
                participant_id=participant_id,
                at=at,
                event=event,
                detail=None if detail is None else detail[:CONTRACT_AUDIT_DETAIL_MAX_CHARS],
            )
        )

    def audit_for_job(self, job_handle: str) -> list[tuple[str, str | None]]:
        rows = self._db.conn.execute(
            select(job_contract_audit.c.event, job_contract_audit.c.detail)
            .where(job_contract_audit.c.job_handle == job_handle)
            .order_by(job_contract_audit.c.id.asc())
        ).fetchall()
        return [(row[0], row[1]) for row in rows]
