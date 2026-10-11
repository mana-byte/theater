"""Custom strict response contracts, keyed by the participant that registered them."""

from __future__ import annotations

from sqlalchemy import Connection, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.persistence.database import Database
from theater.daemon.schema import response_contracts


class ResponseContractRepository:
    """Reads and writes ``response_contracts`` via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def get(
        self, creator_id: str, name: str, *, connection: Connection | None = None
    ) -> str | None:
        conn = self._db.conn if connection is None else connection
        return conn.execute(
            select(response_contracts.c.schema)
            .where(response_contracts.c.creator_id == creator_id)
            .where(response_contracts.c.name == name)
        ).scalar_one_or_none()

    def register(
        self,
        creator_id: str,
        name: str,
        schema: str,
        *,
        created_at: float,
        limit: int,
        connection: Connection,
    ) -> str:
        """``created``, ``same`` (idempotent), ``conflict`` (other schema) or ``full``."""
        existing = self.get(creator_id, name, connection=connection)
        if existing is not None:
            return "same" if existing == schema else "conflict"
        held = connection.execute(
            select(func.count())
            .select_from(response_contracts)
            .where(response_contracts.c.creator_id == creator_id)
        ).scalar_one()
        if held >= limit:
            return "full"
        connection.execute(
            sqlite_insert(response_contracts).values(
                creator_id=creator_id, name=name, schema=schema, created_at=created_at
            )
        )
        return "created"

    def delete_for_creator(self, creator_id: str, *, connection: Connection | None = None) -> None:
        conn = self._db.conn if connection is None else connection
        conn.execute(
            delete(response_contracts).where(response_contracts.c.creator_id == creator_id)
        )
