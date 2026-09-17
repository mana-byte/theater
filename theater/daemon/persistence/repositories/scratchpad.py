"""Machine-wide expiring scratchpad with the legacy private call shape."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from sqlalchemy import BLOB, Connection, cast, delete, func, select, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants import SECONDS_PER_DAY
from theater.constants.daemon import (
    SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE,
    SCRATCHPAD_MAX_VALUE_BYTES,
    SCRATCHPAD_NAMESPACE_QUOTA_BYTES,
    SCRATCHPAD_READ_BUDGET_BYTES,
)
from theater.daemon.persistence.database import Database
from theater.daemon.schema import global_scratchpad
from theater.models import BadRequest, new_id, now

#: Wire bytes of one read response's fixed JSON structure: the wrapper,
#: per-entry separators, and the duplicated after_key key string.
_WIRE_WRAPPER_BYTES = 512
DEFAULT_SCRATCHPAD_TTL_DAYS = 7


def _wire_bytes(text: str) -> int:
    """One string's JSON wire size, quotes and escapes included."""
    return len(json.dumps(text).encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ScratchpadPage:
    """One deterministic, budgeted page of a namespace read."""

    entries: dict[str, str] = field(default_factory=dict)
    keys: tuple[str, ...] = ()
    truncated: bool = False
    after_key: str | None = None
    oversized_key: str | None = None
    oversized_digest: str | None = None
    oversized_bytes: int = 0


class ScratchpadRepository:
    """Reads and writes global ``(namespace, key)`` entries."""

    def __init__(self, db: Database):
        self._db = db

    def _held_bytes(
        self, namespace: str, *, at: float, connection: Connection | None = None
    ) -> int:
        """Raw UTF-8 bytes of keys and values the namespace holds."""
        conn = self._db.conn if connection is None else connection
        total = conn.execute(
            select(
                func.sum(
                    func.length(cast(global_scratchpad.c.key, BLOB))
                    + func.length(cast(global_scratchpad.c.value, BLOB))
                )
            )
            .where(global_scratchpad.c.namespace == namespace)
            .where(global_scratchpad.c.expires_at > at)
        ).scalar()
        return total or 0

    def _entry_bytes(
        self,
        namespace: str,
        key: str,
        *,
        at: float,
        connection: Connection | None = None,
    ) -> int | None:
        """Raw UTF-8 bytes of one stored entry, or ``None`` when it is absent."""
        conn = self._db.conn if connection is None else connection
        size = conn.execute(
            select(
                func.length(cast(global_scratchpad.c.key, BLOB))
                + func.length(cast(global_scratchpad.c.value, BLOB))
            )
            .where(global_scratchpad.c.namespace == namespace)
            .where(global_scratchpad.c.key == key)
            .where(global_scratchpad.c.expires_at > at)
        ).scalar()
        return None if size is None else size

    def _entry_count(
        self, namespace: str, *, at: float, connection: Connection | None = None
    ) -> int:
        """How many entries the namespace already holds."""
        conn = self._db.conn if connection is None else connection
        count = conn.execute(
            select(func.count())
            .select_from(global_scratchpad)
            .where(global_scratchpad.c.namespace == namespace)
            .where(global_scratchpad.c.expires_at > at)
        ).scalar_one()
        return count or 0

    def write(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        value: str,
        updated_by: str | None,
        key: str | None = None,
        actor_client_id: str | None = None,
        ttl_days: float = DEFAULT_SCRATCHPAD_TTL_DAYS,
        connection: Connection | None = None,
    ) -> str:
        del tree_root_id, repo_root
        if key is None:
            key = new_id()
        if isinstance(ttl_days, bool) or not isinstance(ttl_days, (int, float)):
            raise TypeError("scratchpad ttl_days must be a number")
        if not math.isfinite(ttl_days) or ttl_days <= 0:
            raise ValueError("scratchpad ttl_days must be positive and finite")
        value_bytes = len(value.encode("utf-8"))
        if value_bytes > SCRATCHPAD_MAX_VALUE_BYTES:
            raise BadRequest(
                f"scratchpad value is {value_bytes} raw UTF-8 bytes; one entry is "
                f"bounded to {SCRATCHPAD_MAX_VALUE_BYTES} — store a pointer or a "
                "summary, not the payload itself"
            )
        conn = self._db.conn if connection is None else connection
        written_at = now()
        held = self._held_bytes(namespace, at=written_at, connection=conn)
        prior = self._entry_bytes(namespace, key, at=written_at, connection=conn)
        footprint = len(key.encode("utf-8")) + value_bytes
        if held - (prior or 0) + footprint > SCRATCHPAD_NAMESPACE_QUOTA_BYTES:
            raise BadRequest(
                f"scratchpad namespace {namespace!r} holds {held} raw UTF-8 bytes of "
                f"keys and values; this write would exceed the "
                f"{SCRATCHPAD_NAMESPACE_QUOTA_BYTES}-byte aggregate quota — delete "
                "entries or split into another namespace"
            )
        if (
            prior is None
            and self._entry_count(namespace, at=written_at, connection=conn)
            >= SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE
        ):
            raise BadRequest(
                f"scratchpad namespace {namespace!r} already holds "
                f"{SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE} entries; delete entries or "
                "split into another namespace"
            )
        values = {
            "namespace": namespace,
            "key": key,
            "value": value,
            "updated_at": written_at,
            "expires_at": written_at + ttl_days * SECONDS_PER_DAY,
            "actor_client_id": actor_client_id,
            "actor_participant_id": updated_by,
        }
        statement = sqlite_insert(global_scratchpad).values(**values)
        conn.execute(
            statement.on_conflict_do_update(
                index_elements=[global_scratchpad.c.namespace, global_scratchpad.c.key],
                set_={
                    name: value
                    for name, value in values.items()
                    if name not in {"namespace", "key"}
                },
            )
        )
        return key

    def get(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        keys: list[str] | None = None,
        after_key: str | None = None,
        connection: Connection | None = None,
    ) -> ScratchpadPage:
        """One key-ordered page; the budget bounds the encoded wire response."""
        del tree_root_id, repo_root
        conn = self._db.conn if connection is None else connection
        stmt = (
            select(global_scratchpad.c.key, global_scratchpad.c.value)
            .where(global_scratchpad.c.namespace == namespace)
            .where(global_scratchpad.c.expires_at > now())
            .order_by(global_scratchpad.c.key)
        )
        if keys is not None:
            stmt = stmt.where(global_scratchpad.c.key.in_(keys))
        if after_key is not None:
            stmt = stmt.where(global_scratchpad.c.key > after_key)
        entries: dict[str, str] = {}
        used = _wire_bytes(namespace) + _WIRE_WRAPPER_BYTES
        truncated = False
        for row_key, row_value in conn.execute(stmt):
            # A truncated response repeats the last key a third time as
            # after_key, so every row is charged three key copies, the value
            # once, and +3 for their JSON separators.
            cost = 3 * _wire_bytes(row_key) + _wire_bytes(row_value) + 3
            if used + cost > SCRATCHPAD_READ_BUDGET_BYTES:
                if not entries:
                    # The refused entry is named when its key fits the budget
                    # beside the namespace; a digest always names it for
                    # deletion, and the size says what it costs.
                    key_wire = _wire_bytes(row_key)
                    return ScratchpadPage(
                        truncated=True,
                        oversized_key=(
                            row_key
                            if key_wire <= SCRATCHPAD_MAX_VALUE_BYTES
                            and used + key_wire <= SCRATCHPAD_READ_BUDGET_BYTES
                            else None
                        ),
                        oversized_digest=hashlib.sha256(row_key.encode("utf-8")).hexdigest(),
                        oversized_bytes=cost,
                    )
                truncated = True
                break
            entries[row_key] = row_value
            used += cost
        if not entries:
            return ScratchpadPage()
        returned = tuple(entries)
        return ScratchpadPage(
            entries=entries,
            keys=returned,
            truncated=truncated,
            after_key=returned[-1] if truncated else None,
        )

    def delete(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        keys: list[str],
        digests: list[str] | None = None,
        connection: Connection | None = None,
    ) -> list[str]:
        """Delete the named entries, returning the keys that existed; key
        length is unchecked so pre-bound legacy entries stay deletable,
        and digest entries match sha256 of the stored key.
        """
        del tree_root_id, repo_root
        conn = self._db.conn if connection is None else connection
        names = list(keys)
        active = global_scratchpad.c.expires_at > now()
        if digests:
            # A digest names an entry whose key is too large to echo; a
            # digest matching more than one entry would guess, so refuse.
            wanted = frozenset(digests)
            matches: dict[str, list[str]] = {}
            for row in conn.execute(
                select(global_scratchpad.c.key)
                .where(global_scratchpad.c.namespace == namespace)
                .where(active)
            ):
                digest = hashlib.sha256(row[0].encode("utf-8")).hexdigest()
                if digest in wanted:
                    matches.setdefault(digest, []).append(row[0])
            for digest, matched in matches.items():
                if len(matched) > 1:
                    raise BadRequest(
                        f"scratchpad.delete digest {digest} matches {len(matched)} "
                        "entries — refuse to guess; delete them by name"
                    )
            names.extend(k for matched in matches.values() for k in matched)
        existing = [
            row[0]
            for row in conn.execute(
                select(global_scratchpad.c.key)
                .where(global_scratchpad.c.namespace == namespace)
                .where(active)
                .where(global_scratchpad.c.key.in_(names))
                .order_by(global_scratchpad.c.key)
            )
        ]
        if not existing:
            return []
        conn.execute(
            delete(global_scratchpad)
            .where(global_scratchpad.c.namespace == namespace)
            .where(global_scratchpad.c.key.in_(existing))
        )
        return existing

    def clear(self, *, tree_root_id: str, repo_root: str, namespace: str) -> int:
        """Delete every entry the namespace holds, returning how many —
        scoped to the namespace alone, so no key is materialized and an
        unbounded legacy namespace clears without an IN clause.
        """
        del tree_root_id, repo_root
        result = self._db.conn.execute(
            delete(global_scratchpad).where(global_scratchpad.c.namespace == namespace)
        )
        return result.rowcount or 0

    def delete_expired(self, *, before: float, limit: int, connection: Connection) -> int:
        if limit <= 0:
            return 0
        expired = (
            select(global_scratchpad.c.namespace, global_scratchpad.c.key)
            .where(global_scratchpad.c.expires_at <= before)
            .order_by(global_scratchpad.c.expires_at)
            .limit(limit)
        )
        result = connection.execute(
            delete(global_scratchpad).where(
                tuple_(global_scratchpad.c.namespace, global_scratchpad.c.key).in_(expired)
            )
        )
        return int(result.rowcount or 0)
