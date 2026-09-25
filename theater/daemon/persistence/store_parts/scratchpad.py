"""Scratchpad entries and named shared worktree records."""

from __future__ import annotations

from theater.daemon.persistence.repositories.scratchpad import ScratchpadPage
from theater.daemon.persistence.store_parts._host import StoreHost


class ScratchpadStore(StoreHost):
    """Store-facing scratchpad methods; state lives on ``Store``."""

    def scratchpad_write(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        value: str,
        updated_by: str,
        key: str | None = None,
    ) -> str:
        return self._scratchpad.write(
            tree_root_id=tree_root_id,
            repo_root=repo_root,
            namespace=namespace,
            value=value,
            updated_by=updated_by,
            key=key,
        )

    def scratchpad_get(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        keys: list[str] | None = None,
        after_key: str | None = None,
    ) -> ScratchpadPage:
        return self._scratchpad.get(
            tree_root_id=tree_root_id,
            repo_root=repo_root,
            namespace=namespace,
            keys=keys,
            after_key=after_key,
        )

    def scratchpad_delete(
        self,
        *,
        tree_root_id: str,
        repo_root: str,
        namespace: str,
        keys: list[str],
        digests: list[str] | None = None,
    ) -> list[str]:
        return self._scratchpad.delete(
            tree_root_id=tree_root_id,
            repo_root=repo_root,
            namespace=namespace,
            keys=keys,
            digests=digests,
        )

    def scratchpad_delete_expired(self, *, timestamp: float, limit: int) -> int:
        return self._scratchpad.delete_expired(timestamp=timestamp, limit=limit)

    # ---- named worktrees ------------------------------------------------

    def get_named_worktree(self, *, repo_root: str, name: str) -> dict | None:
        return self._worktrees.get(repo_root=repo_root, name=name)

    def upsert_named_worktree(
        self,
        *,
        repo_root: str,
        name: str,
        branch: str,
        path: str,
        base_branch: str | None,
    ) -> None:
        self._worktrees.upsert(
            repo_root=repo_root,
            name=name,
            branch=branch,
            path=path,
            base_branch=base_branch,
        )
