"""Native terminal evidence: exact terminal proof for one native turn.

Keyed by (participant, backend generation, native session, native turn). One
row is sufficient to recover a Theater job after a crash between recording the
evidence and completing the job. First write wins: a re-observed turn does not
rewrite the recorded evidence, and late transcript events must never rewrite
terminal job state.

Persist the evidence *before* completion becomes visible to awaiters. The two
writes are intentionally separate commits in that order: a crash after the
evidence commit and before the job finish is the recoverable crash point
restart reconciliation closes. ``record`` accepts a caller-owned ``connection``
so the evidence can share a transaction with adjacent observation work.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, delete, select, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import RUNTIME_STORAGE_PRUNE_BATCH
from theater.daemon.persistence.database import Database
from theater.daemon.schema import native_terminal_evidence
from theater.harness.contracts.runtime import (
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
)


@dataclass(frozen=True, slots=True)
class NativeTerminalEvidence:
    """The persisted form of one native turn's terminal evidence."""

    participant_id: str
    backend_generation: int
    native_session_id: str
    native_turn_id: str
    terminal: NativeTurnTerminal
    result: str | None = None
    completeness: ResultCompleteness = ResultCompleteness.UNAVAILABLE
    provenance: ResultProvenance = ResultProvenance.NATIVE_EVIDENCE
    error_code: str | None = None
    error: str | None = None
    recorded_at: float = 0.0


class NativeTerminalEvidenceRepository:
    """Reads and writes ``native_terminal_evidence`` via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def record(
        self,
        evidence: NativeTerminalEvidence,
        *,
        connection: Connection | None = None,
    ) -> bool:
        """Persist one terminal evidence row; first write wins.

        Returns whether this call created the row. A repeat recording of the
        same native turn is ignored, not an error: replayed native or durable
        evidence must not repeat completion. Values are validated through the
        public ``NativeTurnOutcome`` contract before persistence, so
        oversized results/errors or malformed identity cannot bypass the
        public bounds; evidence is rejected, never truncated.
        """
        NativeTurnOutcome(
            native_session_id=evidence.native_session_id,
            native_turn_id=evidence.native_turn_id,
            terminal=evidence.terminal,
            result=evidence.result,
            completeness=evidence.completeness,
            provenance=evidence.provenance,
            error_code=evidence.error_code,
            error=evidence.error,
        )
        conn = self._db.conn if connection is None else connection
        result = conn.execute(
            sqlite_insert(native_terminal_evidence)
            .values(**self._values(evidence))
            .on_conflict_do_nothing(
                index_elements=[
                    native_terminal_evidence.c.participant_id,
                    native_terminal_evidence.c.backend_generation,
                    native_terminal_evidence.c.native_session_id,
                    native_terminal_evidence.c.native_turn_id,
                ]
            )
        )
        return bool(result.rowcount)

    def get(
        self,
        *,
        participant_id: str,
        backend_generation: int,
        native_session_id: str,
        native_turn_id: str,
    ) -> NativeTerminalEvidence | None:
        row = self._db.conn.execute(
            select(native_terminal_evidence).where(
                native_terminal_evidence.c.participant_id == participant_id,
                native_terminal_evidence.c.backend_generation == backend_generation,
                native_terminal_evidence.c.native_session_id == native_session_id,
                native_terminal_evidence.c.native_turn_id == native_turn_id,
            )
        ).first()
        return self._from_row(dict(row._mapping)) if row else None

    def for_participant(self, participant_id: str) -> list[NativeTerminalEvidence]:
        """All recorded evidence for one participant, newest first."""
        rows = self._db.conn.execute(
            select(native_terminal_evidence)
            .where(native_terminal_evidence.c.participant_id == participant_id)
            .order_by(native_terminal_evidence.c.recorded_at.desc())
        ).fetchall()
        return [self._from_row(dict(row._mapping)) for row in rows]

    def prune(
        self,
        *,
        older_than: float,
        limit: int = RUNTIME_STORAGE_PRUNE_BATCH,
        connection: Connection | None = None,
    ) -> int:
        """Delete evidence older than a cutoff, bounded by ``limit``.

        Only the GC service may call this, and only once the associated jobs
        are terminal and their recovery/retention obligations have ended.
        """
        if limit <= 0:
            return 0
        conn = self._db.conn if connection is None else connection
        stale = (
            select(
                native_terminal_evidence.c.participant_id,
                native_terminal_evidence.c.backend_generation,
                native_terminal_evidence.c.native_session_id,
                native_terminal_evidence.c.native_turn_id,
            )
            .where(native_terminal_evidence.c.recorded_at < older_than)
            .order_by(native_terminal_evidence.c.recorded_at.asc())
            .limit(limit)
        )
        result = conn.execute(
            delete(native_terminal_evidence).where(
                tuple_(
                    native_terminal_evidence.c.participant_id,
                    native_terminal_evidence.c.backend_generation,
                    native_terminal_evidence.c.native_session_id,
                    native_terminal_evidence.c.native_turn_id,
                ).in_(stale)
            )
        )
        return int(result.rowcount or 0)

    def _values(self, evidence: NativeTerminalEvidence) -> Mapping[str, Any]:
        return {
            "participant_id": evidence.participant_id,
            "backend_generation": evidence.backend_generation,
            "native_session_id": evidence.native_session_id,
            "native_turn_id": evidence.native_turn_id,
            "terminal": str(evidence.terminal),
            "result": evidence.result,
            "result_completeness": str(evidence.completeness),
            "result_provenance": str(evidence.provenance),
            "error_code": evidence.error_code,
            "error": evidence.error,
            "recorded_at": evidence.recorded_at,
        }

    def _from_row(self, row: Mapping[str, Any]) -> NativeTerminalEvidence:
        return NativeTerminalEvidence(
            participant_id=row["participant_id"],
            backend_generation=row["backend_generation"],
            native_session_id=row["native_session_id"],
            native_turn_id=row["native_turn_id"],
            terminal=NativeTurnTerminal(row["terminal"]),
            result=row["result"],
            completeness=ResultCompleteness(row["result_completeness"]),
            provenance=ResultProvenance(row["result_provenance"]),
            error_code=row["error_code"],
            error=row["error"],
            recorded_at=row["recorded_at"],
        )


__all__ = ["NativeTerminalEvidence", "NativeTerminalEvidenceRepository"]
