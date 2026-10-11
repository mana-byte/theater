"""Job completion evidence and deferred-task gates."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, delete, func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.handoff.models import GitFacts, HandoffGate, JobEvidence
from theater.daemon.persistence.database import Database
from theater.daemon.schema import handoff_gates, job_evidence, jobs, touch
from theater.models import Job


class HandoffRepository:
    """Reads and writes ``job_evidence`` and ``handoff_gates`` via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def _conn(self, connection: Connection | None) -> Connection:
        return self._db.conn if connection is None else connection

    # ---- evidence ---------------------------------------------------------

    def put_evidence(self, evidence: JobEvidence, *, connection: Connection | None = None) -> None:
        git = evidence.git
        values = {
            "job_handle": evidence.job_handle,
            "outcome": evidence.outcome,
            "error_code": evidence.error_code,
            "answer": evidence.answer,
            "answer_chars": evidence.answer_chars,
            "answer_source": evidence.answer_source,
            "provenance": evidence.provenance,
            "harness": evidence.harness,
            "participant_id": evidence.participant_id,
            "workspace_id": evidence.workspace_id,
            "cwd": evidence.cwd,
            "branch": git.branch,
            "commit_sha": git.commit_sha,
            "dirty_entries": git.dirty_entries,
            "pin_state": git.pin_state,
            "pin_reason": git.reason,
            "captured_at": evidence.captured_at,
        }
        statement = sqlite_insert(job_evidence).values(**values)
        # First write wins: a source's evidence is pinned once, at its terminal state.
        self._conn(connection).execute(statement.on_conflict_do_nothing())

    def get_evidence(
        self, job_handle: str, *, connection: Connection | None = None
    ) -> JobEvidence | None:
        row = (
            self._conn(connection)
            .execute(select(job_evidence).where(job_evidence.c.job_handle == job_handle))
            .first()
        )
        if row is None:
            return None
        m = row._mapping
        return JobEvidence(
            job_handle=m["job_handle"],
            outcome=m["outcome"],
            error_code=m["error_code"],
            answer=m["answer"],
            answer_chars=m["answer_chars"],
            answer_source=m["answer_source"],
            provenance=m["provenance"],
            harness=m["harness"],
            participant_id=m["participant_id"],
            workspace_id=m["workspace_id"],
            cwd=m["cwd"],
            git=GitFacts(
                pin_state=m["pin_state"],
                commit_sha=m["commit_sha"],
                branch=m["branch"],
                dirty_entries=m["dirty_entries"],
                reason=m["pin_reason"],
            ),
            captured_at=m["captured_at"],
        )

    def delete_evidence(self, handles: Iterable[str], *, connection: Connection | None = None):
        handles = list(handles)
        if handles:
            self._conn(connection).execute(
                delete(job_evidence).where(job_evidence.c.job_handle.in_(handles))
            )

    # ---- gates ------------------------------------------------------------

    def create_gate(self, gate: HandoffGate, *, connection: Connection) -> None:
        connection.execute(
            sqlite_insert(handoff_gates).values(
                job_handle=gate.job_handle,
                caller_id=gate.caller_id,
                source_handle=gate.source_handle,
                source_participant_id=gate.source_participant_id,
                source_name=gate.source_name,
                task_kind=gate.task_kind,
                request=gate.request,
                state=gate.state,
                error_code=gate.error_code,
                error=gate.error,
                created_at=gate.created_at,
                updated_at=gate.updated_at,
            )
        )

    @staticmethod
    def _gate(row) -> HandoffGate:
        m = row._mapping
        return HandoffGate(**{name: m[name] for name in HandoffGate.__dataclass_fields__})

    def get_gate(self, job_handle: str, *, connection: Connection | None = None):
        row = (
            self._conn(connection)
            .execute(select(handoff_gates).where(handoff_gates.c.job_handle == job_handle))
            .first()
        )
        return None if row is None else self._gate(row)

    def gates_in_states(
        self, states: Iterable[str], *, source_handle: str | None = None
    ) -> list[HandoffGate]:
        stmt = select(handoff_gates).where(handoff_gates.c.state.in_(list(states)))
        if source_handle is not None:
            stmt = stmt.where(handoff_gates.c.source_handle == source_handle)
        rows = self._db.conn.execute(stmt.order_by(handoff_gates.c.created_at)).fetchall()
        return [self._gate(row) for row in rows]

    def count_open_for_caller(self, caller_id: str, *, connection: Connection | None = None) -> int:
        return int(
            self._conn(connection)
            .execute(
                select(func.count())
                .select_from(handoff_gates)
                .where(handoff_gates.c.caller_id == caller_id)
                .where(handoff_gates.c.state.in_(["waiting", "opening"]))
            )
            .scalar_one()
        )

    def move_gate(
        self,
        job_handle: str,
        *,
        expected: Iterable[str],
        state: str,
        updated_at: float,
        error_code: str | None = None,
        error: str | None = None,
        connection: Connection | None = None,
    ) -> bool:
        """Compare-and-set the state; False when another writer moved the gate first."""
        result = self._conn(connection).execute(
            update(handoff_gates)
            .where(handoff_gates.c.job_handle == job_handle)
            .where(handoff_gates.c.state.in_(list(expected)))
            .values(state=state, updated_at=updated_at, error_code=error_code, error=error)
        )
        return result.rowcount == 1

    def delete_gates(self, handles: Iterable[str], *, connection: Connection | None = None):
        handles = list(handles)
        if handles:
            self._conn(connection).execute(
                delete(handoff_gates).where(
                    handoff_gates.c.job_handle.in_(handles),
                    handoff_gates.c.state.in_(["opened", "failed", "cancelled"]),
                )
            )

    # ---- source lookups ----------------------------------------------------

    def newest_turn_job(self, target_id: str) -> Job | None:
        """The newest spawn or send job that carried a task; a promptless launch never counts."""
        row = self._db.conn.execute(
            select(jobs)
            .where(jobs.c.target_id == target_id)
            .where(jobs.c.kind.in_(["spawn", "send"]))
            .where(jobs.c.prompt.is_not(None))
            .where(jobs.c.prompt != "")
            .order_by(jobs.c.created_at.desc(), jobs.c.handle.desc())
            .limit(1)
        ).first()
        return None if row is None else Job.from_row(row._mapping)

    def modified_paths(self, job_handle: str, *, limit: int) -> list[str]:
        rows = self._db.conn.execute(
            select(touch.c.path)
            .where(touch.c.job_handle == job_handle)
            .where(touch.c.mode == "write")
            .order_by(touch.c.id)
        ).fetchall()
        return list(dict.fromkeys(row[0] for row in rows))[:limit]
