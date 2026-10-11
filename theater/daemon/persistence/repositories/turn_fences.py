"""Durable turn fences: native turns whose late end must answer no newer job."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.observation import FENCED_TURNS
from theater.daemon.persistence.database import Database
from theater.daemon.schema import turn_fences


class TurnFenceRepository:
    def __init__(self, db: Database):
        self._db = db

    def keys(self, participant_id: str, *, connection: Connection | None = None) -> tuple[str, ...]:
        """The participant's fences, oldest first."""
        conn = self._db.conn if connection is None else connection
        rows = conn.execute(
            select(turn_fences.c.fence)
            .where(turn_fences.c.participant_id == participant_id)
            .order_by(turn_fences.c.created_at, turn_fences.c.fence)
        ).all()
        return tuple(row.fence for row in rows)

    def add(
        self,
        participant_id: str,
        fences: Iterable[str],
        *,
        job_handle: str | None,
        created_at: float,
        connection: Connection,
    ) -> None:
        """Insert fences once each, then keep only the newest FENCED_TURNS for the participant."""
        for fence in dict.fromkeys(fences):
            connection.execute(
                sqlite_insert(turn_fences)
                .values(
                    participant_id=participant_id,
                    fence=fence,
                    job_handle=job_handle,
                    created_at=created_at,
                )
                .on_conflict_do_nothing()
            )
        kept = (
            select(turn_fences.c.fence)
            .where(turn_fences.c.participant_id == participant_id)
            .order_by(turn_fences.c.created_at.desc(), turn_fences.c.fence.desc())
            .limit(FENCED_TURNS)
        )
        connection.execute(
            delete(turn_fences).where(
                turn_fences.c.participant_id == participant_id,
                turn_fences.c.fence.not_in(kept),
            )
        )
