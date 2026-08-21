"""Meta key/value store and the durable send-sequence counter."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import SEND_SEQ_META_KEY
from theater.daemon.persistence.database import Database
from theater.daemon.schema import meta


class MetadataRepository:
    """Reads and writes the ``meta`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def get(self, key: str) -> str | None:
        row = self._db.conn.execute(select(meta.c.value).where(meta.c.key == key)).first()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        stmt = sqlite_insert(meta).values(key=key, value=value)
        self._db.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[meta.c.key],
                set_={"value": value},
            )
        )

    def get_send_seq(self) -> int:
        raw = self.get(SEND_SEQ_META_KEY)
        if raw is None:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def set_send_seq(self, value: int) -> None:
        self.set(SEND_SEQ_META_KEY, str(value))
