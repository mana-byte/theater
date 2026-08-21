"""Named shared worktree persistence."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from theater.daemon.persistence.database import Database
from theater.daemon.schema import named_worktrees
from theater.models import now


class WorktreeRepository:
    """Reads and writes the ``named_worktrees`` table via ``db.conn``."""

    def __init__(self, db: Database):
        self._db = db

    def get(self, *, repo_root: str, name: str) -> dict | None:
        row = self._db.conn.execute(
            select(named_worktrees)
            .where(named_worktrees.c.repo_root == repo_root)
            .where(named_worktrees.c.name == name)
        ).first()
        return dict(row._mapping) if row else None

    def upsert(
        self,
        *,
        repo_root: str,
        name: str,
        branch: str,
        path: str,
        base_branch: str | None,
    ) -> None:
        stmt = sqlite_insert(named_worktrees).values(
            repo_root=repo_root,
            name=name,
            branch=branch,
            path=path,
            base_branch=base_branch,
            created_at=now(),
        )
        self._db.conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[named_worktrees.c.repo_root, named_worktrees.c.name],
                set_={
                    "branch": branch,
                    "path": path,
                    "base_branch": base_branch,
                },
            )
        )

    def delete(self, *, repo_root: str, name: str) -> None:
        self._db.conn.execute(
            named_worktrees.delete()
            .where(named_worktrees.c.repo_root == repo_root)
            .where(named_worktrees.c.name == name)
        )

    def by_path(self, path: str) -> dict | None:
        row = self._db.conn.execute(
            select(named_worktrees).where(named_worktrees.c.path == path)
        ).first()
        return dict(row._mapping) if row else None
