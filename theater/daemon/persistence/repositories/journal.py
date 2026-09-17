"""Durable orchestration journal allocation and transaction grouping."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any

from sqlalchemy import Connection, delete, func, insert, select, update

from theater.daemon.persistence.database import Database
from theater.daemon.persistence.repositories._json import decode_json, encode_json
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


@dataclass(frozen=True, slots=True)
class JournalRetainedHead:
    """The first retained row, used to reject cursors across retention gaps."""

    sequence: int
    event_index: int
    ending_sequence: int


@dataclass(frozen=True, slots=True)
class JournalReadEvent:
    """One decoded durable event belonging to a complete journal group."""

    sequence: int
    kind: str
    entity_id: str
    entity_revision: int
    payload: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class JournalReadGroup:
    """A complete persisted transaction group in sequence order."""

    transaction_id: str
    first_sequence: int
    ending_sequence: int
    events: tuple[JournalReadEvent, ...]


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

    def retained_head(self, *, connection: Connection | None = None) -> JournalRetainedHead | None:
        """Return the oldest retained row without deriving a stream sequence from it."""
        conn = self._db.conn if connection is None else connection
        row = conn.execute(
            select(
                orchestration_events.c.sequence,
                orchestration_events.c.event_index,
                orchestration_events.c.ending_sequence,
            )
            .order_by(orchestration_events.c.sequence.asc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return JournalRetainedHead(
            sequence=int(row.sequence),
            event_index=int(row.event_index),
            ending_sequence=int(row.ending_sequence),
        )

    def has_ending_sequence(self, sequence: int, *, connection: Connection | None = None) -> bool:
        """Return whether ``sequence`` is a retained complete-group boundary."""
        conn = self._db.conn if connection is None else connection
        return (
            conn.execute(
                select(orchestration_events.c.sequence)
                .where(orchestration_events.c.ending_sequence == sequence)
                .limit(1)
            ).first()
            is not None
        )

    def groups_after(
        self,
        sequence: int,
        *,
        limit: int,
        connection: Connection | None = None,
    ) -> tuple[JournalReadGroup, ...]:
        """Read at most ``limit`` whole retained groups after a validated cursor."""
        if limit < 1:
            raise ValueError("journal group limit must be positive")
        conn = self._db.conn if connection is None else connection
        groups = conn.execute(
            select(
                orchestration_events.c.transaction_id,
                orchestration_events.c.ending_sequence,
                func.min(orchestration_events.c.sequence).label("first_sequence"),
                func.count().label("event_count"),
            )
            .where(orchestration_events.c.ending_sequence > sequence)
            .group_by(
                orchestration_events.c.transaction_id,
                orchestration_events.c.ending_sequence,
            )
            .order_by(func.min(orchestration_events.c.sequence).asc())
            .limit(limit)
        ).all()
        return tuple(
            self._read_group(
                str(group.transaction_id),
                int(group.ending_sequence),
                int(group.first_sequence),
                int(group.event_count),
                connection=conn,
            )
            for group in groups
        )

    def delete_through(self, sequence: int, *, connection: Connection) -> int:
        result = connection.execute(
            delete(orchestration_events).where(orchestration_events.c.sequence <= sequence)
        )
        return int(result.rowcount or 0)

    @staticmethod
    def _read_group(
        transaction_id: str,
        ending_sequence: int,
        first_sequence: int,
        event_count: int,
        *,
        connection: Connection,
    ) -> JournalReadGroup:
        rows = connection.execute(
            select(orchestration_events)
            .where(orchestration_events.c.transaction_id == transaction_id)
            .where(orchestration_events.c.ending_sequence == ending_sequence)
            .order_by(orchestration_events.c.sequence.asc())
        ).all()
        if not rows or len(rows) != event_count or event_count > MAX_EVENTS_PER_TRANSACTION:
            raise ValueError("stored journal transaction is incomplete or exceeds its event limit")
        if ending_sequence != first_sequence + event_count - 1:
            raise ValueError("stored journal transaction has a non-contiguous sequence")
        events: list[JournalReadEvent] = []
        for index, row in enumerate(rows):
            values: dict[str, Any] = dict(row._mapping)
            if (
                int(values["sequence"]) != first_sequence + index
                or int(values["event_index"]) != index
                or int(values["ending_sequence"]) != ending_sequence
            ):
                raise ValueError("stored journal transaction has invalid event ordering")
            try:
                payload = decode_json(str(values["payload"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("stored journal event payload is invalid") from exc
            if not isinstance(payload, dict):
                raise TypeError("stored journal event payload must be an object")
            events.append(
                JournalReadEvent(
                    sequence=int(values["sequence"]),
                    kind=str(values["kind"]),
                    entity_id=str(values["entity_id"]),
                    entity_revision=int(values["entity_revision"]),
                    payload=payload,
                )
            )
        return JournalReadGroup(
            transaction_id=transaction_id,
            first_sequence=first_sequence,
            ending_sequence=ending_sequence,
            events=tuple(events),
        )

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
    "JournalReadEvent",
    "JournalReadGroup",
    "JournalRepository",
    "JournalRetainedHead",
]
