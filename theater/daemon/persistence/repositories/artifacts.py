"""Durable ownership records for participant-generated filesystem artifacts."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import Connection, delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.artifacts import ArtifactKind, OwnedArtifact, validate_persisted_path
from theater.daemon.persistence.database import Database
from theater.daemon.schema import participant_artifacts


class ArtifactRepository:
    """Persist launch ownership until filesystem cleanup has succeeded."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add_many(
        self,
        participant_id: str,
        artifacts: Sequence[OwnedArtifact],
        *,
        connection: Connection | None = None,
    ) -> None:
        if not artifacts:
            return
        conn = self._db.conn if connection is None else connection
        for artifact in artifacts:
            path = validate_persisted_path(
                artifact.path,
                owner_id=participant_id,
                kind=artifact.kind,
            )
            conn.execute(
                sqlite_insert(participant_artifacts)
                .values(
                    participant_id=participant_id,
                    path=str(path),
                    kind=artifact.kind.value,
                )
                .on_conflict_do_update(
                    index_elements=[
                        participant_artifacts.c.participant_id,
                        participant_artifacts.c.path,
                    ],
                    set_={"kind": artifact.kind.value},
                )
            )

    def list_for(self, participant_id: str) -> tuple[OwnedArtifact, ...]:
        rows = self._db.conn.execute(
            select(participant_artifacts.c.path, participant_artifacts.c.kind)
            .where(participant_artifacts.c.participant_id == participant_id)
            .order_by(participant_artifacts.c.path)
        ).fetchall()
        result: list[OwnedArtifact] = []
        for path, raw_kind in rows:
            try:
                kind = ArtifactKind(raw_kind)
            except ValueError as exc:
                raise ValueError(
                    f"participant artifact {path!r} has unknown kind {raw_kind!r}"
                ) from exc
            result.append(OwnedArtifact(Path(path), kind))
        return tuple(result)

    def owner_ids(self) -> tuple[str, ...]:
        rows = self._db.conn.execute(
            select(participant_artifacts.c.participant_id)
            .distinct()
            .order_by(participant_artifacts.c.participant_id)
        ).fetchall()
        return tuple(row[0] for row in rows)

    def delete_for(self, participant_id: str, *, connection: Connection | None = None) -> int:
        conn = self._db.conn if connection is None else connection
        result = conn.execute(
            delete(participant_artifacts).where(
                participant_artifacts.c.participant_id == participant_id
            )
        )
        return result.rowcount


__all__ = ["ArtifactRepository"]
