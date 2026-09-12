"""Tree-scoped scratchpad key/value store, bounded on write and read."""

from __future__ import annotations

import hashlib
import json
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

#: Wire bytes of one read response's fixed JSON structure: the wrapper,
#: per-entry separators, and the duplicated after_key key string.
_WIRE_WRAPPER_BYTES = 512


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
    """Reads and writes the ``tree_kv`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def _held_bytes(self, tree_root_id: str, repo_root: str, namespace: str) -> int:
        """Raw UTF-8 bytes of keys and values the namespace holds."""
        total = self._db.conn.execute(
            select(
                func.sum(
                    func.length(cast(tree_kv.c.key, BLOB))
                    + func.length(cast(tree_kv.c.value, BLOB))
                )
            )
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
        ).scalar()
        return total or 0

    def _entry_bytes(
        self, tree_root_id: str, repo_root: str, namespace: str, key: str
    ) -> int | None:
        """Raw UTF-8 bytes of one stored entry, or ``None`` when it is absent."""
        size = self._db.conn.execute(
            select(
                func.length(cast(tree_kv.c.key, BLOB)) + func.length(cast(tree_kv.c.value, BLOB))
            )
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
                f"scratchpad value is {value_bytes} raw UTF-8 bytes; one entry is "
                f"bounded to {SCRATCHPAD_MAX_VALUE_BYTES} — store a pointer or a "
                "summary, not the payload itself"
            )
        held = self._held_bytes(tree_root_id, repo_root, namespace)
        prior = self._entry_bytes(tree_root_id, repo_root, namespace, key)
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
        """One key-ordered page; the budget bounds the encoded wire response."""
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
        used = _wire_bytes(namespace) + _WIRE_WRAPPER_BYTES
        truncated = False
        for row_key, row_value in self._db.conn.execute(stmt):
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
    ) -> list[str]:
        """Delete the named entries, returning the keys that existed; key
        length is unchecked so pre-bound legacy entries stay deletable,
        and digest entries match sha256 of the stored key.
        """
        names = list(keys)
        if digests:
            # A digest names an entry whose key is too large to echo; a
            # digest matching more than one entry would guess, so refuse.
            wanted = frozenset(digests)
            matches: dict[str, list[str]] = {}
            for row in self._db.conn.execute(
                select(tree_kv.c.key)
                .where(tree_kv.c.tree_root_id == tree_root_id)
                .where(tree_kv.c.repo_root == repo_root)
                .where(tree_kv.c.namespace == namespace)
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
            for row in self._db.conn.execute(
                select(tree_kv.c.key)
                .where(tree_kv.c.tree_root_id == tree_root_id)
                .where(tree_kv.c.repo_root == repo_root)
                .where(tree_kv.c.namespace == namespace)
                .where(tree_kv.c.key.in_(names))
                .order_by(tree_kv.c.key)
            )
        ]
        if not existing:
            return []
        self._db.conn.execute(
            delete(tree_kv)
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
            .where(tree_kv.c.key.in_(existing))
        )
        return existing

    def clear(self, *, tree_root_id: str, repo_root: str, namespace: str) -> int:
        """Delete every entry the namespace holds, returning how many."""
        keys = [
            row[0]
            for row in self._db.conn.execute(
                select(tree_kv.c.key)
                .where(tree_kv.c.tree_root_id == tree_root_id)
                .where(tree_kv.c.repo_root == repo_root)
                .where(tree_kv.c.namespace == namespace)
            )
        ]
        if keys:
            self._db.conn.execute(
                delete(tree_kv)
                .where(tree_kv.c.tree_root_id == tree_root_id)
                .where(tree_kv.c.repo_root == repo_root)
                .where(tree_kv.c.namespace == namespace)
                .where(tree_kv.c.key.in_(keys))
            )
        return len(keys)
