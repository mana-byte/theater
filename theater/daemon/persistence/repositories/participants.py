"""Participant CRUD, status, lineage, and cwd queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.persistence.database import Database
from theater.daemon.schema import participants
from theater.models import Participant, Status, Tier, now


class ParticipantRepository:
    """Reads and writes the ``participants`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    @staticmethod
    def _participant_values(p: Participant) -> dict:
        return {
            "id": p.id,
            "harness": p.harness,
            "tier": str(p.tier),
            "tmux_pane": p.tmux_pane,
            "cwd": p.cwd,
            "branch": p.branch,
            "session_id": p.session_id,
            "session_correlation": p.session_correlation,
            "transcript_domain": p.transcript_domain,
            "transcript_location": p.transcript_location,
            "resume_floor": p.resume_floor,
            "parent_id": p.parent_id,
            "pid": p.pid,
            "status": str(p.status),
            "last_activity": p.last_activity,
            "created_at": p.created_at,
        }

    def upsert(self, p: Participant) -> None:
        values = self._participant_values(p)
        stmt = sqlite_insert(participants).values(**values)
        self._db.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[participants.c.id],
                set_={k: v for k, v in values.items() if k != "id"},
            )
        )

    def get(self, pid: str) -> Participant | None:
        row = self._db.conn.execute(select(participants).where(participants.c.id == pid)).first()
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
        stmt = stmt.order_by(participants.c.created_at.asc())
        return [Participant.from_row(r._mapping) for r in self._db.conn.execute(stmt)]

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

    def touch(self, pid: str) -> None:
        self._db.conn.execute(
            update(participants).where(participants.c.id == pid).values(last_activity=now())
        )

    def clear_resume_floor(self, pid: str) -> None:
        """Clear the resume floor column without touching any other field."""
        self._db.conn.execute(
            update(participants).where(participants.c.id == pid).values(resume_floor=None)
        )

    def reparent(self, pid: str, *, new_parent_id: str) -> None:
        """Set the parent_id of a participant."""
        self._db.conn.execute(
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
        """Count matching ``Participant.addressable``: tier != EXTERNAL and status != DEAD."""
        return int(
            self._db.conn.execute(
                select(func.count())
                .select_from(participants)
                .where(
                    participants.c.tier != str(Tier.EXTERNAL),
                    participants.c.status != str(Status.DEAD),
                )
            ).scalar_one()
        )
