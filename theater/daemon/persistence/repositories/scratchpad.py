"""Global, TTL-aware scratchpad persistence with bounded reads and writes."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from sqlalchemy import BLOB, Connection, cast, delete, func, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import (
    SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE,
    SCRATCHPAD_MAX_VALUE_BYTES,
    SCRATCHPAD_NAMESPACE_QUOTA_BYTES,
    SCRATCHPAD_READ_BUDGET_BYTES,
)
from theater.daemon.persistence.database import Database
from theater.daemon.schema import global_scratchpad
from theater.models import BadRequest, new_id, now

DEFAULT_TTL_SECONDS = 7 * 24 * 60 * 60

#: Wire bytes of one read response's fixed JSON structure: the wrapper,
#: per-entry separators, and the duplicated after_key key string.
_WIRE_WRAPPER_BYTES = 512


def _wire_bytes(text: str) -> int:
    """One string's JSON wire size, quotes and escapes included."""
    return len(json.dumps(text).encode("utf-8"))


def _finite_timestamp(value: float, label: str) -> float:
    timestamp = float(value)
    if not math.isfinite(timestamp):
        raise ValueError(f"scratchpad {label} must be finite")
    return timestamp


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


@dataclass(frozen=True, slots=True)
class ScratchpadNamespacePage:
    """One key-ordered page of live namespace names."""

    namespaces: tuple[str, ...] = ()
    next_cursor: str | None = None


class ScratchpadRepository:
    """Reads and writes the machine-wide ``global_scratchpad`` table."""

    def __init__(self, db: Database):
        self._db = db

    @staticmethod
    def _live(namespace: str, timestamp: float):
        return (global_scratchpad.c.namespace == namespace) & (
            global_scratchpad.c.expires_at > timestamp
        )

    @staticmethod
    def _held_bytes(connection: Connection, namespace: str, timestamp: float) -> int:
        """Raw UTF-8 bytes of unexpired keys and values in one namespace."""
        total = connection.execute(
            select(
                func.sum(
                    func.length(cast(global_scratchpad.c.key, BLOB))
                    + func.length(cast(global_scratchpad.c.value, BLOB))
                )
            ).where(ScratchpadRepository._live(namespace, timestamp))
        ).scalar()
        return int(total or 0)

    @staticmethod
    def _entry_bytes(
        connection: Connection, namespace: str, key: str, timestamp: float
    ) -> int | None:
        """Raw bytes of a live entry, or ``None`` when it is absent or expired."""
        size = connection.execute(
            select(
                func.length(cast(global_scratchpad.c.key, BLOB))
                + func.length(cast(global_scratchpad.c.value, BLOB))
            )
            .where(ScratchpadRepository._live(namespace, timestamp))
            .where(global_scratchpad.c.key == key)
        ).scalar()
        return None if size is None else int(size)

    @staticmethod
    def _entry_count(connection: Connection, namespace: str, timestamp: float) -> int:
        """How many live entries the namespace already holds."""
        count = connection.execute(
            select(func.count())
            .select_from(global_scratchpad)
            .where(ScratchpadRepository._live(namespace, timestamp))
        ).scalar_one()
        return int(count or 0)

    def write(
        self,
        *,
        namespace: str,
        value: str,
        key: str | None = None,
        updated_at: float | None = None,
        expires_at: float | None = None,
        actor_client_id: str | None = None,
        actor_participant_id: str | None = None,
        connection: Connection | None = None,
        tree_root_id: str | None = None,
        repo_root: str | None = None,
        updated_by: str | None = None,
    ) -> str:
        """Atomically enforce live quotas and upsert one global entry.

        The ignored legacy scope arguments keep the RC9 Store compatibility
        façade callable until its coordinator-owned composition is replaced.
        """
        del tree_root_id, repo_root
        timestamp = _finite_timestamp(now() if updated_at is None else updated_at, "updated_at")
        expiry = _finite_timestamp(
            timestamp + DEFAULT_TTL_SECONDS if expires_at is None else expires_at,
            "expires_at",
        )
        participant_id = actor_participant_id if actor_participant_id is not None else updated_by
        if key is None:
            key = new_id()
        if connection is None:
            with self._db.write_unit() as unit:
                return self._write(
                    unit.connection,
                    namespace=namespace,
                    value=value,
                    key=key,
                    updated_at=timestamp,
                    expires_at=expiry,
                    actor_client_id=actor_client_id,
                    actor_participant_id=participant_id,
                )
        return self._write(
            connection,
            namespace=namespace,
            value=value,
            key=key,
            updated_at=timestamp,
            expires_at=expiry,
            actor_client_id=actor_client_id,
            actor_participant_id=participant_id,
        )

    @staticmethod
    def _write(
        connection: Connection,
        *,
        namespace: str,
        value: str,
        key: str,
        updated_at: float,
        expires_at: float,
        actor_client_id: str | None,
        actor_participant_id: str | None,
    ) -> str:
        value_bytes = len(value.encode("utf-8"))
        if value_bytes > SCRATCHPAD_MAX_VALUE_BYTES:
            raise BadRequest(
                f"scratchpad value is {value_bytes} raw UTF-8 bytes; one entry is "
                f"bounded to {SCRATCHPAD_MAX_VALUE_BYTES} — store a pointer or a "
                "summary, not the payload itself"
            )
        # SQLite begins read transactions lazily. This no-op write takes the
        # database writer slot before quota reads, so separate connections do
        # not validate the same stale namespace total concurrently.
        connection.execute(
            update(global_scratchpad)
            .where(global_scratchpad.c.namespace == namespace)
            .values(updated_at=global_scratchpad.c.updated_at)
        )
        held = ScratchpadRepository._held_bytes(connection, namespace, updated_at)
        prior = ScratchpadRepository._entry_bytes(connection, namespace, key, updated_at)
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
            and ScratchpadRepository._entry_count(connection, namespace, updated_at)
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
            "updated_at": updated_at,
            "expires_at": expires_at,
            "actor_client_id": actor_client_id,
            "actor_participant_id": actor_participant_id,
        }
        statement = sqlite_insert(global_scratchpad).values(**values)
        connection.execute(
            statement.on_conflict_do_update(
                index_elements=(global_scratchpad.c.namespace, global_scratchpad.c.key),
                set_={name: values[name] for name in values if name not in {"namespace", "key"}},
            )
        )
        return key

    def get(
        self,
        *,
        namespace: str,
        keys: list[str] | None = None,
        after_key: str | None = None,
        limit: int | None = None,
        timestamp: float | None = None,
        connection: Connection | None = None,
        tree_root_id: str | None = None,
        repo_root: str | None = None,
    ) -> ScratchpadPage:
        """Read one key-ordered page of live entries within the wire budget."""
        del tree_root_id, repo_root
        if limit is not None and limit < 1:
            raise ValueError("scratchpad page limit must be positive")
        observed_at = _finite_timestamp(now() if timestamp is None else timestamp, "read timestamp")
        conn = self._db.conn if connection is None else connection
        stmt = (
            select(global_scratchpad.c.key, global_scratchpad.c.value)
            .where(self._live(namespace, observed_at))
            .order_by(global_scratchpad.c.key)
        )
        if keys is not None:
            stmt = stmt.where(global_scratchpad.c.key.in_(keys))
        if after_key is not None:
            stmt = stmt.where(global_scratchpad.c.key > after_key)
        if limit is not None:
            stmt = stmt.limit(limit + 1)
        entries: dict[str, str] = {}
        used = _wire_bytes(namespace) + _WIRE_WRAPPER_BYTES
        truncated = False
        for row_key, row_value in conn.execute(stmt):
            if limit is not None and len(entries) >= limit:
                truncated = True
                break
            # A truncated response repeats the last key a third time as
            # after_key, so every row is charged three key copies, the value
            # once, and +3 for their JSON separators.
            cost = 3 * _wire_bytes(row_key) + _wire_bytes(row_value) + 3
            if used + cost > SCRATCHPAD_READ_BUDGET_BYTES:
                if not entries:
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

    def namespaces(
        self,
        *,
        after_namespace: str | None = None,
        limit: int = 200,
        timestamp: float | None = None,
        connection: Connection | None = None,
    ) -> ScratchpadNamespacePage:
        """Return a page of namespace names with at least one live entry."""
        if limit < 1:
            raise ValueError("scratchpad namespace page limit must be positive")
        observed_at = _finite_timestamp(now() if timestamp is None else timestamp, "read timestamp")
        conn = self._db.conn if connection is None else connection
        stmt = (
            select(global_scratchpad.c.namespace)
            .where(global_scratchpad.c.expires_at > observed_at)
            .distinct()
            .order_by(global_scratchpad.c.namespace)
            .limit(limit + 1)
        )
        if after_namespace is not None:
            stmt = stmt.where(global_scratchpad.c.namespace > after_namespace)
        names = tuple(conn.execute(stmt).scalars())
        page = names[:limit]
        return ScratchpadNamespacePage(
            namespaces=page,
            next_cursor=page[-1] if len(names) > limit and page else None,
        )

    def delete(
        self,
        *,
        namespace: str,
        keys: list[str],
        digests: list[str] | None = None,
        connection: Connection | None = None,
        tree_root_id: str | None = None,
        repo_root: str | None = None,
    ) -> list[str]:
        """Delete named entries, retaining digest cleanup for legacy oversized keys."""
        del tree_root_id, repo_root
        if connection is None:
            with self._db.write_unit() as unit:
                return self._delete(
                    unit.connection, namespace=namespace, keys=keys, digests=digests
                )
        return self._delete(connection, namespace=namespace, keys=keys, digests=digests)

    @staticmethod
    def _delete(
        connection: Connection,
        *,
        namespace: str,
        keys: list[str],
        digests: list[str] | None,
    ) -> list[str]:
        names = list(keys)
        if digests:
            wanted = frozenset(digests)
            matches: dict[str, list[str]] = {}
            for row_key in connection.execute(
                select(global_scratchpad.c.key).where(global_scratchpad.c.namespace == namespace)
            ).scalars():
                digest = hashlib.sha256(row_key.encode("utf-8")).hexdigest()
                if digest in wanted:
                    matches.setdefault(digest, []).append(row_key)
            for digest, matched in matches.items():
                if len(matched) > 1:
                    raise BadRequest(
                        f"scratchpad.delete digest {digest} matches {len(matched)} "
                        "entries — refuse to guess; delete them by name"
                    )
            names.extend(key for matched in matches.values() for key in matched)
        existing = list(
            connection.execute(
                select(global_scratchpad.c.key)
                .where(global_scratchpad.c.namespace == namespace)
                .where(global_scratchpad.c.key.in_(names))
                .order_by(global_scratchpad.c.key)
            ).scalars()
        )
        if not existing:
            return []
        connection.execute(
            delete(global_scratchpad)
            .where(global_scratchpad.c.namespace == namespace)
            .where(global_scratchpad.c.key.in_(existing))
        )
        return existing

    def clear(
        self,
        *,
        namespace: str,
        connection: Connection | None = None,
        tree_root_id: str | None = None,
        repo_root: str | None = None,
    ) -> int:
        """Delete every physical row in a namespace, including expired cleanup rows."""
        del tree_root_id, repo_root
        if connection is None:
            with self._db.write_unit() as unit:
                return self._clear(unit.connection, namespace=namespace)
        return self._clear(connection, namespace=namespace)

    @staticmethod
    def _clear(connection: Connection, *, namespace: str) -> int:
        result = connection.execute(
            delete(global_scratchpad).where(global_scratchpad.c.namespace == namespace)
        )
        return int(result.rowcount or 0)

    def delete_expired(
        self,
        *,
        timestamp: float | None = None,
        limit: int = 200,
        connection: Connection | None = None,
    ) -> int:
        """Physically delete at most ``limit`` logically expired rows."""
        if limit < 1:
            raise ValueError("scratchpad expiry deletion limit must be positive")
        observed_at = _finite_timestamp(
            now() if timestamp is None else timestamp, "expiry timestamp"
        )
        if connection is None:
            with self._db.write_unit() as unit:
                return self._delete_expired(unit.connection, observed_at, limit)
        return self._delete_expired(connection, observed_at, limit)

    @staticmethod
    def _delete_expired(connection: Connection, timestamp: float, limit: int) -> int:
        rows = connection.execute(
            select(global_scratchpad.c.namespace, global_scratchpad.c.key)
            .where(global_scratchpad.c.expires_at <= timestamp)
            .order_by(
                global_scratchpad.c.expires_at,
                global_scratchpad.c.namespace,
                global_scratchpad.c.key,
            )
            .limit(limit)
        ).all()
        if not rows:
            return 0
        identities = [(str(row.namespace), str(row.key)) for row in rows]
        result = connection.execute(
            delete(global_scratchpad).where(
                tuple_(global_scratchpad.c.namespace, global_scratchpad.c.key).in_(identities)
            ).where(global_scratchpad.c.expires_at <= timestamp)
        )
        return int(result.rowcount or 0)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "_WIRE_WRAPPER_BYTES",
    "ScratchpadNamespacePage",
    "ScratchpadPage",
    "ScratchpadRepository",
    "_wire_bytes",
]
