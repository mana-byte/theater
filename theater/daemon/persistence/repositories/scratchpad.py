"""Tree-scoped scratchpad key/value store."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.persistence.database import Database
from theater.daemon.schema import tree_kv
from theater.models import new_id, now


class ScratchpadRepository:
    """Reads and writes the ``tree_kv`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

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
            stmt = sqlite_insert(tree_kv).values(
                tree_root_id=tree_root_id,
                repo_root=repo_root,
                namespace=namespace,
                key=key,
                value=value,
                updated_at=now(),
                updated_by=updated_by,
            )
            self._db.conn.execute(stmt)
        else:
            existing = self._db.conn.execute(
                select(tree_kv.c.key)
                .where(tree_kv.c.tree_root_id == tree_root_id)
                .where(tree_kv.c.repo_root == repo_root)
                .where(tree_kv.c.namespace == namespace)
                .where(tree_kv.c.key == key)
            ).first()
            if existing:
                self._db.conn.execute(
                    tree_kv.update()
                    .where(tree_kv.c.tree_root_id == tree_root_id)
                    .where(tree_kv.c.repo_root == repo_root)
                    .where(tree_kv.c.namespace == namespace)
                    .where(tree_kv.c.key == key)
                    .values(value=value, updated_at=now(), updated_by=updated_by)
                )
            else:
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
        return key

    def get(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        keys: list[str] | None = None,
    ) -> dict[str, str]:
        stmt = (
            select(tree_kv.c.key, tree_kv.c.value)
            .where(tree_kv.c.tree_root_id == tree_root_id)
            .where(tree_kv.c.repo_root == repo_root)
            .where(tree_kv.c.namespace == namespace)
        )
        if keys is not None:
            stmt = stmt.where(tree_kv.c.key.in_(keys))
        rows = self._db.conn.execute(stmt).fetchall()
        return {row[0]: row[1] for row in rows}
