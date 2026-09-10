"""Meta key/value store and the durable send-sequence counter."""

from __future__ import annotations

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import SEND_SEQ_META_KEY
from theater.daemon.persistence.database import Database
from theater.daemon.schema import meta


class MetadataRepository:
    """Reads and writes the ``meta`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def get(self, key: str, *, connection: Connection | None = None) -> str | None:
        conn = self._db.conn if connection is None else connection
        row = conn.execute(select(meta.c.value).where(meta.c.key == key)).first()
        return row[0] if row else None

    def set(self, key: str, value: str, *, connection: Connection | None = None) -> None:
        stmt = sqlite_insert(meta).values(key=key, value=value)
        conn = self._db.conn if connection is None else connection
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[meta.c.key],
                set_={"value": value},
            )
        )

    def get_send_seq(self, *, connection: Connection | None = None) -> int:
        raw = self.get(SEND_SEQ_META_KEY, connection=connection)
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def set_send_seq(self, value: int) -> None:
        self.set(SEND_SEQ_META_KEY, str(value))

    def allocate_send_seq(self, *, connection: Connection | None = None) -> int:
        """Atomically increment and persist the durable sequence counter.

        The single allocator for job handles and followup queue positions.
        The counter lives in ``meta``, independent of any GC-prunable rows,
        and is never derived from ``MAX(...)``, timestamps, or memory. When a
        caller-owned ``connection`` is given, both the read and the write go
        through that connection so the increment is atomic within the
        caller's transaction.
        """
        conn = self._db.conn if connection is None else connection
        value = self.get_send_seq(connection=conn) + 1
        stmt = sqlite_insert(meta).values(key=SEND_SEQ_META_KEY, value=str(value))
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[meta.c.key],
                set_={"value": str(value)},
            )
        )
        return value
