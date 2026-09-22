"""Bounded reads of the durable orchestration journal."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import Connection

from theater.daemon.persistence.repositories.journal import JournalRepository
from theater.frontend.capabilities import MAX_FRAME_BYTES

_FOLLOW_RESPONSE_BYTES = MAX_FRAME_BYTES - 4096


class StateReadError(Exception):
    """A bounded public-state refusal without coupling journal reads to routing."""

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = dict(details) if details is not None else None
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class StreamCursor:
    stream_id: str
    sequence: int

    def to_wire(self) -> dict[str, object]:
        return {"stream_id": self.stream_id, "sequence": self.sequence}


@dataclass(frozen=True, slots=True)
class JournalBatch:
    transactions: tuple[dict[str, object], ...]
    cursor: StreamCursor


class JournalReader:
    """Validate cursors and return only complete public transaction groups."""

    def __init__(
        self,
        journal: JournalRepository,
        *,
        response_bytes: int = _FOLLOW_RESPONSE_BYTES,
    ) -> None:
        if response_bytes < 1:
            raise ValueError("journal response byte limit must be positive")
        self._journal = journal
        self._response_bytes = response_bytes

    def cursor(self, *, connection: Connection | None = None) -> StreamCursor:
        return StreamCursor(
            stream_id=self._journal.stream_id(connection=connection),
            sequence=self._journal.current_sequence(connection=connection),
        )

    def read(
        self,
        cursor: StreamCursor,
        *,
        limit: int,
        connection: Connection | None = None,
    ) -> JournalBatch:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise StateReadError("bad_request", "state follow limit must be between 1 and 500")
        self._validate_cursor(cursor, connection=connection)
        stream_id = self._journal.stream_id(connection=connection)
        groups = self._journal.groups_after(cursor.sequence, limit=limit, connection=connection)
        transactions: list[dict[str, object]] = []
        encoded_bytes = 0
        ending = cursor.sequence
        for group in groups:
            transaction: dict[str, object] = {
                "transaction_id": group.transaction_id,
                "events": [
                    {
                        "kind": event.kind,
                        "entity_id": event.entity_id,
                        "entity_revision": event.entity_revision,
                        "payload": dict(event.payload),
                    }
                    for event in group.events
                ],
                "ending_cursor": {"stream_id": stream_id, "sequence": group.ending_sequence},
            }
            size = _json_bytes(transaction)
            if size > self._response_bytes:
                raise StateReadError(
                    "too_large",
                    "one journal transaction exceeds the public state response limit",
                    {"limit_bytes": self._response_bytes},
                )
            if encoded_bytes + size > self._response_bytes:
                break
            transactions.append(transaction)
            encoded_bytes += size
            ending = group.ending_sequence
        return JournalBatch(tuple(transactions), StreamCursor(stream_id, ending))

    def _validate_cursor(self, cursor: StreamCursor, *, connection: Connection | None) -> None:
        if type(cursor.sequence) is not int or cursor.sequence < 0:
            raise StateReadError(
                "bad_request", "state cursor sequence must be a non-negative integer"
            )
        stream_id = self._journal.stream_id(connection=connection)
        if cursor.stream_id != stream_id:
            raise StateReadError(
                "resnapshot_required",
                "the state cursor belongs to a different database stream",
                {"reason": "stream_mismatch", "stream_id": stream_id},
            )
        current = self._journal.current_sequence(connection=connection)
        if cursor.sequence > current:
            raise StateReadError(
                "resnapshot_required",
                "the state cursor is ahead of this durable stream",
                {"reason": "cursor_ahead", "current_sequence": current},
            )
        head = self._journal.retained_head(connection=connection)
        if head is None:
            if cursor.sequence != current:
                raise StateReadError(
                    "resnapshot_required",
                    "state events required by this cursor have expired",
                    {"reason": "retention_gap", "current_sequence": current},
                )
            return
        if cursor.sequence < head.sequence - 1:
            raise StateReadError(
                "resnapshot_required",
                "state events required by this cursor have expired",
                {"reason": "retention_gap", "first_retained_sequence": head.sequence},
            )
        if cursor.sequence == head.sequence - 1:
            if head.event_index == 0:
                return
            raise StateReadError(
                "resnapshot_required",
                "state retention begins inside a transaction group",
                {"reason": "retention_gap", "first_retained_sequence": head.sequence},
            )
        if cursor.sequence == current or self._journal.has_ending_sequence(
            cursor.sequence, connection=connection
        ):
            return
        raise StateReadError(
            "resnapshot_required",
            "the state cursor is not a complete transaction boundary",
            {"reason": "incomplete_transaction_cursor"},
        )


def _json_bytes(value: object) -> int:
    try:
        return len(json.dumps(value, allow_nan=False, separators=(",", ":")).encode())
    except (TypeError, ValueError) as exc:
        raise StateReadError("internal", "stored journal data cannot be encoded publicly") from exc


__all__ = ["JournalBatch", "JournalReader", "StateReadError", "StreamCursor"]
