"""Tree-scoped scratchpad key/value store, bounded on write and read."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import BLOB, cast, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.constants.daemon import (
    SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE,
    SCRATCHPAD_MAX_VALUE_BYTES,
    SCRATCHPAD_NAMESPACE_QUOTA_BYTES,
    SCRATCHPAD_READ_BUDGET_BYTES,
)
from theater.daemon.persistence.database import Database
from theater.daemon.schema import tree_kv
from theater.models import BadRequest, new_id, now


@dataclass(frozen=True, slots=True)
class ScratchpadPage:
    """One deterministic, budgeted page of a namespace read."""

    entries: dict[str, str] = field(default_factory=dict)
    keys: tuple[str, ...] = ()
    truncated: bool = False
    after_key: str | None = None


class ScratchpadRepository:
    """Reads and writes the ``tree_kv`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def _held_bytes(self, tree_root_id: str, repo_root: str, namespace: str) -> int:
        """Aggregate encoded bytes the namespace already holds."""
        total = self._db.conn.execute(
            select(func.sum(func.length(cast(tree_kv.c.value, BLOB))))
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
        ).scalar()
        return total or 0

    def _entry_bytes(
        self, tree_root_id: str, repo_root: str, namespace: str, key: str
    ) -> int | None:
        """Encoded bytes of one existing entry, or ``None`` when it is absent."""
        size = self._db.conn.execute(
            select(func.length(cast(tree_kv.c.value, BLOB)))
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
            .where(tree_kv.c.key == key)
        ).scalar()
        return None if size is None else size

    def _entry_count(self, tree_root_id: str, repo_root: str, namespace: str) -> int:
        """How many entries the namespace already holds."""
        count = self._db.conn.execute(
            select(func.count())
            .select_from(tree_kv)
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
        ).scalar_one()
        return count or 0

    def write(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        value: str,
        updated_by: str,
        key: str | None = None,
    ) -> str:
        if key is None:
            key = new_id()
        value_bytes = len(value.encode("utf-8"))
        if value_bytes > SCRATCHPAD_MAX_VALUE_BYTES:
            raise BadRequest(
                f"scratchpad value is {value_bytes} encoded bytes; one entry is bounded "
                f"to {SCRATCHPAD_MAX_VALUE_BYTES} — store a pointer or a summary, not "
                "the payload itself"
            )
        held = self._held_bytes(tree_root_id, repo_root, namespace)
        prior = self._entry_bytes(tree_root_id, repo_root, namespace, key)
        if held - (prior or 0) + value_bytes > SCRATCHPAD_NAMESPACE_QUOTA_BYTES:
            raise BadRequest(
                f"scratchpad namespace {namespace!r} holds {held} encoded bytes; this "
                f"write would exceed the {SCRATCHPAD_NAMESPACE_QUOTA_BYTES}-byte "
                "aggregate quota — delete entries or split into another namespace"
            )
        if (
            prior is None
            and self._entry_count(tree_root_id, repo_root, namespace)
            >= SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE
        ):
            raise BadRequest(
                f"scratchpad namespace {namespace!r} already holds "
                f"{SCRATCHPAD_MAX_ENTRIES_PER_NAMESPACE} entries; delete entries or "
                "split into another namespace"
            )
        if prior is None:
            self._db.conn.execute(
                sqlite_insert(tree_kv).values(
                    tree_root_id=tree_root_id,
                    repo_root=repo_root,
                    namespace=namespace,
                    key=key,
                    value=value,
                    updated_at=now(),
                    updated_by=updated_by,
                )
            )
        else:
            self._db.conn.execute(
                tree_kv.update()
                .where(tree_kv.c.tree_root_id == tree_root_id)
                .where(tree_kv.c.repo_root == repo_root)
                .where(tree_kv.c.namespace == namespace)
                .where(tree_kv.c.key == key)
                .values(value=value, updated_at=now(), updated_by=updated_by)
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
    ) -> ScratchpadPage:
        """One key-ordered page; the budget bounds what one read returns."""
        stmt = (
            select(tree_kv.c.key, tree_kv.c.value)
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
            .order_by(tree_kv.c.key)
        )
        if keys is not None:
            stmt = stmt.where(tree_kv.c.key.in_(keys))
        if after_key is not None:
            stmt = stmt.where(tree_kv.c.key > after_key)
        entries: dict[str, str] = {}
        used = 0
        truncated = False
        for row_key, row_value in self._db.conn.execute(stmt):
            cost = len(row_key.encode("utf-8")) + len(row_value.encode("utf-8"))
            if entries and used + cost > SCRATCHPAD_READ_BUDGET_BYTES:
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

    def delete(self, *, tree_root_id: str, repo_root: str, namespace: str, key: str) -> bool:
        """Delete one entry; report whether it existed (idempotent)."""
        result = self._db.conn.execute(
            delete(tree_kv)
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
            .where(tree_kv.c.key == key)
        )
        return bool(result.rowcount)
