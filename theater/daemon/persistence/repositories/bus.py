"""Bus append, tail, and observation-error queries."""

from __future__ import annotations

import json
from collections.abc import Collection

from sqlalchemy import Connection, insert, or_, select

from theater.constants.daemon import (
    BUS_KIND_AGENT_TRANSCRIPT,
    BUS_KIND_AGENT_TRANSCRIPT_RECEIPT,
    BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
    BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
    BUS_KIND_SEND_REFUSED,
    BUS_PARTICIPANT_PAGE_MAX_LIMIT,
    TRANSCRIPT_AUDIT_KINDS,
)
from theater.daemon.persistence.database import Database
from theater.daemon.schema import bus
from theater.models import now


class BusRepository:
    """Reads and writes the ``bus`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def append(
        self,
        kind: str,
        *,
        from_id: str | None = None,
        to_id: str | None = None,
        payload: dict | None = None,
        timestamp: float | None = None,
        connection: Connection | None = None,
    ) -> int:
        conn = self._db.conn if connection is None else connection
        result = conn.execute(
            insert(bus).values(
                ts=now() if timestamp is None else timestamp,
                from_id=from_id,
                to_id=to_id,
                kind=kind,
                payload=json.dumps(payload) if payload else None,
            )
        )
        pk = result.inserted_primary_key
        assert pk is not None
        return pk[0]

    @staticmethod
    def _decode(row) -> dict:
        event = dict(row._mapping)
        event["payload"] = json.loads(event["payload"]) if event["payload"] else None
        return event

    def tail(self, limit: int = 100, *, after_id: int = 0) -> list[dict]:
        rows = self._db.conn.execute(
            select(bus).where(bus.c.id > after_id).order_by(bus.c.id.desc()).limit(limit)
        ).fetchall()
        return [self._decode(row) for row in reversed(rows)]

    def page_for_participant(
        self,
        participant_id: str,
        *,
        before_id: int | str | None = None,
        limit: int = BUS_PARTICIPANT_PAGE_MAX_LIMIT,
        kinds: Collection[str],
    ) -> list[dict]:
        """Return one bounded, ascending page of allowlisted participant events."""
        if isinstance(limit, bool) or not isinstance(limit, int):
            raise ValueError("limit must be an integer")  # noqa: TRY004
        if limit < 0:
            raise ValueError("limit must be non-negative")
        bounded_limit = min(limit, BUS_PARTICIPANT_PAGE_MAX_LIMIT)
        cursor: int | None = None
        if before_id is not None:
            if isinstance(before_id, bool) or not isinstance(before_id, (int, str)):
                raise ValueError("before_id must be an integer cursor")
            try:
                cursor = int(before_id)
            except (TypeError, ValueError, OverflowError):
                raise ValueError("before_id must be an integer cursor") from None
            if cursor < 0:
                raise ValueError("before_id must be a non-negative integer cursor")
        if not participant_id or bounded_limit == 0 or not kinds:
            return []

        query = (
            select(bus)
            .where(or_(bus.c.from_id == participant_id, bus.c.to_id == participant_id))
            .where(bus.c.kind.in_(tuple(kinds)))
        )
        if cursor is not None:
            query = query.where(bus.c.id < cursor)
        rows = self._db.conn.execute(
            query.order_by(bus.c.id.desc()).limit(bounded_limit)
        ).fetchall()
        return [self._decode(row) for row in reversed(rows)]

    def record_for_participant(
        self,
        participant_id: str,
        row_id: int,
        *,
        kinds: Collection[str],
    ) -> dict | None:
        if not participant_id or type(row_id) is not int or row_id < 0 or not kinds:
            return None
        row = self._db.conn.execute(
            select(bus)
            .where(bus.c.id == row_id)
            .where(or_(bus.c.from_id == participant_id, bus.c.to_id == participant_id))
            .where(bus.c.kind.in_(tuple(kinds)))
            .limit(1)
        ).fetchone()
        return self._decode(row) if row is not None else None

    def refusal_counts(self, *, since: float | None = None) -> dict[str, int]:
        """Sends refused before a job existed, counted by reason."""
        query = select(bus.c.payload).where(bus.c.kind == BUS_KIND_SEND_REFUSED)
        if since is not None:
            query = query.where(bus.c.ts >= since)
        counts: dict[str, int] = {}
        for (payload,) in self._db.conn.execute(query).fetchall():
            try:
                reason = json.loads(payload or "{}").get("reason") or "unknown"
            except ValueError:
                reason = "unknown"
            counts[reason] = counts.get(reason, 0) + 1
        return counts

    def observation_error_active(self, participant_id: str, code: str) -> bool:
        """Whether an observation error remains uncleared in the audit stream."""
        return self._observation_error_row(participant_id, code) is not None

    def observation_error_timestamp(self, participant_id: str, code: str) -> float | None:
        """The wall-clock ``ts`` of the most recent uncleared observation error."""
        row = self._observation_error_row(participant_id, code)
        return row["ts"] if row is not None else None

    def _observation_error_row(self, participant_id: str, code: str) -> dict | None:
        rows = self._db.conn.execute(
            select(bus.c.kind, bus.c.payload, bus.c.ts)
            .where(bus.c.to_id == participant_id)
            .where(bus.c.kind.in_(tuple(TRANSCRIPT_AUDIT_KINDS)))
            .order_by(bus.c.id.desc())
        ).fetchall()
        for row in rows:
            kind = row.kind
            payload = row.payload
            if kind in {
                BUS_KIND_AGENT_TRANSCRIPT,
                BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
                BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
            }:
                return None
            try:
                decoded = json.loads(payload or "{}")
            except ValueError:
                continue
            if kind == BUS_KIND_AGENT_TRANSCRIPT_RECEIPT and decoded.get("admission") == "accepted":
                return None
            found = decoded.get("code")
            if found == code:
                return {"ts": row.ts, "kind": kind, "payload": decoded}
        return None
