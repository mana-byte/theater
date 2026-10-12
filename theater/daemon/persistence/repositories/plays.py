"""Play instances, held positions, and pending position bindings."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, delete, insert, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.persistence.database import Database
from theater.daemon.schema import participants, play_assignments, play_instances, play_members
from theater.models import Status, new_id
from theater.skills import PlayRules


@dataclass(frozen=True, slots=True)
class PlayMembership:
    """One position a participant holds in a live play, with the play's pinned rules."""

    participant_id: str
    instance_id: str
    master_id: str
    position: str
    rules: PlayRules

    @property
    def play(self) -> str:
        return self.rules.play

    @property
    def is_master(self) -> bool:
        return self.participant_id == self.master_id


@dataclass(frozen=True, slots=True)
class PlayAssignment:
    """A message bound to the position its target holds, or is given once it is delivered."""

    job_handle: str
    participant_id: str
    instance_id: str
    position: str
    assigned_by: str


class PlayRepository:
    """Rows are keyed by play instance, so a reused master id never revives an ended play."""

    def __init__(self, db: Database):
        self._db = db

    def _conn(self, connection: Connection | None) -> Connection:
        return self._db.conn if connection is None else connection

    def active_membership(
        self, participant_id: str, *, connection: Connection | None = None
    ) -> PlayMembership | None:
        """The position held in a play whose master is not dead; history never counts."""
        row = (
            self._conn(connection)
            .execute(
                select(
                    play_members.c.participant_id,
                    play_members.c.instance_id,
                    play_instances.c.master_id,
                    play_members.c.position,
                    play_instances.c.rules,
                )
                .join(play_instances, play_instances.c.instance_id == play_members.c.instance_id)
                .join(participants, participants.c.id == play_instances.c.master_id)
                .where(play_members.c.participant_id == participant_id)
                .where(participants.c.status != Status.DEAD.value)
                .order_by(play_members.c.created_at.desc())
            )
            .first()
        )
        if row is None:
            return None
        return PlayMembership(
            participant_id=row.participant_id,
            instance_id=row.instance_id,
            master_id=row.master_id,
            position=row.position,
            rules=PlayRules.from_json(row.rules),
        )

    def live_master(self, instance_id: str, *, connection: Connection | None = None) -> str | None:
        """The master of a play instance that is still live, else None."""
        row = (
            self._conn(connection)
            .execute(
                select(play_instances.c.master_id)
                .join(participants, participants.c.id == play_instances.c.master_id)
                .where(play_instances.c.instance_id == instance_id)
                .where(participants.c.status != Status.DEAD.value)
            )
            .first()
        )
        return None if row is None else str(row.master_id)

    def start(
        self,
        master_id: str,
        rules: PlayRules,
        *,
        created_at: float,
        connection: Connection,
    ) -> str:
        """Pin the rules and seat the master in one transaction; returns the instance id."""
        instance_id = new_id()
        connection.execute(
            insert(play_instances).values(
                instance_id=instance_id,
                master_id=master_id,
                play=rules.play,
                rules=rules.to_json(),
                rules_sha256=rules.sha256,
                created_at=created_at,
            )
        )
        self.add_member(
            master_id, instance_id, rules.master, created_at=created_at, connection=connection
        )
        return instance_id

    def add_member(
        self,
        participant_id: str,
        instance_id: str,
        position: str,
        *,
        created_at: float,
        connection: Connection,
    ) -> None:
        connection.execute(
            sqlite_insert(play_members)
            .values(
                participant_id=participant_id,
                instance_id=instance_id,
                position=position,
                created_at=created_at,
            )
            .on_conflict_do_nothing()
        )

    def put_assignment(
        self, assignment: PlayAssignment, *, created_at: float, connection: Connection | None = None
    ) -> None:
        self._conn(connection).execute(
            sqlite_insert(play_assignments)
            .values(
                job_handle=assignment.job_handle,
                participant_id=assignment.participant_id,
                instance_id=assignment.instance_id,
                position=assignment.position,
                assigned_by=assignment.assigned_by,
                created_at=created_at,
            )
            .on_conflict_do_nothing()
        )

    def assignment(
        self, job_handle: str, *, connection: Connection | None = None
    ) -> PlayAssignment | None:
        row = (
            self._conn(connection)
            .execute(select(play_assignments).where(play_assignments.c.job_handle == job_handle))
            .first()
        )
        if row is None:
            return None
        return PlayAssignment(
            job_handle=row.job_handle,
            participant_id=row.participant_id,
            instance_id=row.instance_id,
            position=row.position,
            assigned_by=row.assigned_by,
        )

    def delete_assignment(self, job_handle: str, *, connection: Connection | None = None) -> None:
        self._conn(connection).execute(
            delete(play_assignments).where(play_assignments.c.job_handle == job_handle)
        )


__all__ = ["PlayAssignment", "PlayMembership", "PlayRepository"]
