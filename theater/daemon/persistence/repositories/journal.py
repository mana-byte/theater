"""Durable orchestration journal allocation and transaction grouping."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from sqlalchemy import Connection, delete, insert, select, update

from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories._json import encode_json
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.schema import meta, orchestration_events
from theater.frontend.dto.events import EVENT_KINDS
from theater.models import JournalEventRecord, new_id

STREAM_ID_META_KEY = "orchestration_stream_id"
SEQUENCE_META_KEY = "orchestration_sequence"
MAX_EVENTS_PER_TRANSACTION = 500


@dataclass(frozen=True, slots=True)
class JournalAppend:
    transaction_id: str
    stream_id: str
    first_sequence: int
    ending_sequence: int


class JournalRepository:
    def __init__(self, db: Database):
        self._db = db
        self._listeners: list[Callable[[int], None]] = []

    def register_listener(self, listener: Callable[[int], None]) -> None:
        self._listeners.append(listener)

    def append_group(
        self,
        unit: WriteUnit,
        events: Sequence[JournalEventRecord],
        *,
        transaction_id: str | None = None,
    ) -> JournalAppend:
        if not events or len(events) > MAX_EVENTS_PER_TRANSACTION:
            raise ValueError("journal transaction must contain between 1 and 500 events")
        if any(event.kind not in EVENT_KINDS for event in events):
            raise ValueError("journal event kind is not in the public RC10 catalog")
        if any(event.entity_revision < 0 for event in events):
            raise ValueError("journal entity revisions must be non-negative")
        conn = unit.connection
        first, ending = self._allocate(len(events), connection=conn)
        group_id = transaction_id or new_id()
        for index, event in enumerate(events):
            conn.execute(
                insert(orchestration_events).values(
                    sequence=first + index,
                    transaction_id=group_id,
                    event_index=index,
                    ending_sequence=ending,
                    kind=event.kind,
                    entity_id=event.entity_id,
                    entity_revision=event.entity_revision,
                    payload=encode_json(dict(event.payload)),
                    recorded_at=event.recorded_at,
                )
            )
        for listener in tuple(self._listeners):
            unit.after_commit(partial(listener, ending))
        return JournalAppend(
            transaction_id=group_id,
            stream_id=self.stream_id(connection=conn),
            first_sequence=first,
            ending_sequence=ending,
        )

    def stream_id(self, *, connection: Connection | None = None) -> str:
        conn = self._db.conn if connection is None else connection
        value = conn.execute(
            select(meta.c.value).where(meta.c.key == STREAM_ID_META_KEY)
        ).scalar_one()
        return str(value)

    def current_sequence(self, *, connection: Connection | None = None) -> int:
        conn = self._db.conn if connection is None else connection
        value = conn.execute(
            select(meta.c.value).where(meta.c.key == SEQUENCE_META_KEY)
        ).scalar_one()
        return int(value)

    def delete_through(self, sequence: int, *, connection: Connection) -> int:
        result = connection.execute(
            delete(orchestration_events).where(orchestration_events.c.sequence <= sequence)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def _allocate(count: int, *, connection: Connection) -> tuple[int, int]:
        current = int(
            connection.execute(
                select(meta.c.value).where(meta.c.key == SEQUENCE_META_KEY)
            ).scalar_one()
        )
        ending = current + count
        connection.execute(
            update(meta).where(meta.c.key == SEQUENCE_META_KEY).values(value=str(ending))
        )
        return current + 1, ending


__all__ = [
    "MAX_EVENTS_PER_TRANSACTION",
    "SEQUENCE_META_KEY",
    "STREAM_ID_META_KEY",
    "JournalAppend",
    "JournalRepository",
]
