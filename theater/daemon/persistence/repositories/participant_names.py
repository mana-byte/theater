"""The persisted names of live participants."""

from __future__ import annotations

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.persistence.database import Database
from theater.daemon.schema import participant_names, participants
from theater.models import Status


class ParticipantNameRepository:
    """Reads and writes ``participant_names``; liveness comes from ``participants.status``."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def live(self) -> list[tuple[str, str]]:
        """(participant id, name) of non-dead participants, oldest first so duplicates resolve."""
        rows = self._db.conn.execute(
            select(participant_names.c.participant_id, participant_names.c.name)
            .join(participants, participants.c.id == participant_names.c.participant_id)
            .where(participants.c.status != str(Status.DEAD))
            .order_by(participants.c.created_at, participants.c.id)
        ).fetchall()
        return [(row[0], row[1]) for row in rows]

    def set(self, participant_id: str, name: str, *, connection: Connection | None = None) -> None:
        conn = self._db.conn if connection is None else connection
        conn.execute(
            sqlite_insert(participant_names)
            .values(participant_id=participant_id, name=name)
            .on_conflict_do_update(
                index_elements=[participant_names.c.participant_id], set_={"name": name}
            )
        )

    def delete(self, participant_id: str, *, connection: Connection | None = None) -> None:
        conn = self._db.conn if connection is None else connection
        conn.execute(
            delete(participant_names).where(participant_names.c.participant_id == participant_id)
        )

    def purge_stale(self, *, connection: Connection | None = None) -> None:
        """Drop rows whose participant is dead or gone."""
        live = select(participants.c.id).where(participants.c.status != str(Status.DEAD))
        conn = self._db.conn if connection is None else connection
        conn.execute(
            delete(participant_names).where(participant_names.c.participant_id.not_in(live))
        )
