"""Participant CRUD, status, lineage, and cwd queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Connection, and_, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import TMUX_RESTART_TERMINATION_REASON
from theater.daemon.persistence.database import Database
from theater.daemon.schema import participant_runtime_bindings, participants, terminal_bindings
from theater.models import ControlOwnerKind, Participant, ParticipantOrigin, Status, now


class ParticipantRepository:
    """Reads and writes the ``participants`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    @staticmethod
    def _participant_values(p: Participant) -> dict:
        origin = p.origin or ParticipantOrigin(str(p.tier))
        owner_kind = p.control_owner_kind
        if owner_kind is None:
            owner_kind = (
                ControlOwnerKind.PARTICIPANT
                if p.parent_id is not None
                else ControlOwnerKind.LOCAL_OPERATOR
            )
        owner_id = p.control_owner_id
        if owner_kind is ControlOwnerKind.PARTICIPANT and owner_id is None:
            owner_id = p.parent_id
        if owner_kind is ControlOwnerKind.LOCAL_OPERATOR:
            owner_id = None
        return {
            "id": p.id,
            "harness": p.harness,
            "tier": str(p.tier),
            "tmux_pane": p.tmux_pane,
            "tmux_server_identity": p.tmux_server_identity,
            "termination_reason": p.termination_reason,
            "termination_incident": p.termination_incident,
            "terminated_at": p.terminated_at,
            "cwd": p.cwd,
            "branch": p.branch,
            "session_id": p.session_id,
            "session_correlation": p.session_correlation,
            "transcript_domain": p.transcript_domain,
            "transcript_location": p.transcript_location,
            "resume_floor": p.resume_floor,
            "source_checkpoint": p.source_checkpoint,
            "resumed_from_id": p.resumed_from_id,
            "parent_id": p.parent_id,
            "pid": p.pid,
            "status": str(p.status),
            "last_activity": p.last_activity,
            "created_at": p.created_at,
            "description": p.description,
            "origin": str(origin),
            "control_owner_kind": str(owner_kind),
            "control_owner_id": owner_id,
            "control_revision": p.control_revision,
            "workspace_id": p.workspace_id,
        }

    def upsert(self, p: Participant, *, connection: Connection | None = None) -> None:
        values = self._participant_values(p)
        stmt = sqlite_insert(participants).values(**values)
        conn = self._db.conn if connection is None else connection
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[participants.c.id],
                set_={k: v for k, v in values.items() if k not in {"id", "origin", "parent_id"}},
            )
        )

    def get(self, pid: str, *, connection: Connection | None = None) -> Participant | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(select(participants).where(participants.c.id == pid)).first()
        return Participant.from_row(row._mapping) if row else None

    def find_by_pane(self, pane: str) -> Participant | None:
        row = self._db.conn.execute(
            select(participants)
            .where(participants.c.tmux_pane == pane)
            .where(participants.c.status != str(Status.DEAD))
            .order_by(participants.c.created_at.desc())
            .limit(1)
        ).first()
        return Participant.from_row(row._mapping) if row else None

    def list_all(
        self,
        *,
        include_dead: bool = False,
        ids: Sequence[str] | None = None,
        parent_id: str | None = None,
        after: tuple[float, str] | None = None,
        limit: int | None = None,
        connection: Connection | None = None,
    ) -> list[Participant]:
        stmt = select(participants)
        if not include_dead:
            stmt = stmt.where(participants.c.status != str(Status.DEAD))
        if ids is not None:
            if not ids:
                return []
            stmt = stmt.where(participants.c.id.in_(ids))
        if parent_id is not None:
            stmt = stmt.where(participants.c.parent_id == parent_id)
        if after is not None:
            created_at, participant_id = after
            stmt = stmt.where(
                or_(
                    participants.c.created_at > created_at,
                    and_(
                        participants.c.created_at == created_at,
                        participants.c.id > participant_id,
                    ),
                )
            )
        stmt = stmt.order_by(participants.c.created_at.asc(), participants.c.id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        conn = self._db.conn if connection is None else connection
        return [Participant.from_row(r._mapping) for r in conn.execute(stmt)]

    def list_recent_dead(
        self, *, limit: int = 20, exclude_session_ids: set[str] | None = None
    ) -> list[Participant]:
        stmt = (
            select(participants)
            .where(participants.c.status == str(Status.DEAD))
            .where(participants.c.session_id.is_not(None))
            .where(participants.c.session_id != "")
        )
        if exclude_session_ids:
            stmt = stmt.where(participants.c.session_id.not_in(exclude_session_ids))
        stmt = stmt.order_by(
            participants.c.last_activity.desc(),
            participants.c.created_at.desc(),
            participants.c.id.desc(),
        ).limit(limit)
        return [Participant.from_row(r._mapping) for r in self._db.conn.execute(stmt)]

    def children_of(self, pid: str) -> list[Participant]:
        stmt = (
            select(participants)
            .where(participants.c.parent_id == pid)
            .order_by(participants.c.created_at)
        )
        return [Participant.from_row(r._mapping) for r in self._db.conn.execute(stmt)]

    def set_status(self, pid: str, status: Status) -> None:
        self._db.conn.execute(
            update(participants)
            .where(participants.c.id == pid)
            .values(status=str(status), last_activity=now())
        )

    def stamp_live_tmux_server_identity(
        self,
        identity: str,
        *,
        participant_ids: Sequence[str] | None = None,
        connection: Connection | None = None,
    ) -> int:
        if participant_ids is not None and not participant_ids:
            return 0
        stmt = (
            update(participants)
            .where(participants.c.status != str(Status.DEAD))
            .where(participants.c.tmux_pane.is_not(None))
            .where(participants.c.tmux_server_identity.is_(None))
            .values(tmux_server_identity=identity)
        )
        if participant_ids is not None:
            stmt = stmt.where(participants.c.id.in_(participant_ids))
        conn = self._db.conn if connection is None else connection
        result = conn.execute(stmt)
        return result.rowcount

    def mark_tmux_restarted(
        self,
        participant_ids: Sequence[str],
        *,
        incident: str,
        terminated_at: float,
        connection: Connection | None = None,
    ) -> int:
        if not participant_ids:
            return 0
        conn = self._db.conn if connection is None else connection
        result = conn.execute(
            update(participants)
            .where(participants.c.id.in_(participant_ids))
            .where(participants.c.status != str(Status.DEAD))
            .values(
                status=str(Status.DEAD),
                termination_reason=TMUX_RESTART_TERMINATION_REASON,
                termination_incident=incident,
                terminated_at=terminated_at,
                last_activity=terminated_at,
            )
        )
        return result.rowcount

    def touch(self, pid: str) -> None:
        self._db.conn.execute(
            update(participants).where(participants.c.id == pid).values(last_activity=now())
        )

    def clear_resume_floor(self, pid: str) -> None:
        """Clear the resume floor column without touching any other field."""
        self._db.conn.execute(
            update(participants).where(participants.c.id == pid).values(resume_floor=None)
        )

    def set_source_checkpoint(self, pid: str, checkpoint: str) -> None:
        self._db.conn.execute(
            update(participants)
            .where(participants.c.id == pid)
            .where(participants.c.source_checkpoint.is_not(checkpoint))
            .values(source_checkpoint=checkpoint)
        )

    def reparent(
        self,
        pid: str,
        *,
        new_parent_id: str,
        connection: Connection | None = None,
    ) -> None:
        """Set the parent_id of a participant."""
        conn = self._db.conn if connection is None else connection
        conn.execute(
            update(participants).where(participants.c.id == pid).values(parent_id=new_parent_id)
        )

    def live_in_cwd(self, cwd: str) -> list[Participant]:
        rows = self._db.conn.execute(
            select(participants)
            .where(participants.c.cwd == cwd)
            .where(participants.c.status != str(Status.DEAD))
        ).fetchall()
        return [Participant.from_row(r._mapping) for r in rows]

    def live_count(self) -> int:
        """Count of participants whose status is not DEAD."""
        return int(
            self._db.conn.execute(
                select(func.count())
                .select_from(participants)
                .where(participants.c.status != str(Status.DEAD))
            ).scalar_one()
        )

    def addressable_count(self) -> int:
        """Count live participants with a current terminal or native route."""
        terminal_exists = (
            select(terminal_bindings.c.participant_id)
            .where(terminal_bindings.c.participant_id == participants.c.id)
            .where(terminal_bindings.c.health == "healthy")
            .exists()
        )
        native_exists = (
            select(participant_runtime_bindings.c.participant_id)
            .where(participant_runtime_bindings.c.participant_id == participants.c.id)
            .where(participant_runtime_bindings.c.wiring == "native")
            .where(
                participant_runtime_bindings.c.lifecycle_phase.in_(("bound", "attached", "active"))
            )
            .exists()
        )
        return int(
            self._db.conn.execute(
                select(func.count())
                .select_from(participants)
                .where(participants.c.status != str(Status.DEAD))
                .where(terminal_exists | native_exists)
            ).scalar_one()
        )
