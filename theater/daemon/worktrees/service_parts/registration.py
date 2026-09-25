"""Workspace registration, lookup, listing, and projection."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Connection

from theater.daemon import workers
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.worktrees.identity import (
    inspect_existing_path,
)
from theater.daemon.worktrees.service_parts._common import (
    WorkspaceNotFound,
)
from theater.daemon.worktrees.service_parts._host import WorkspaceHost
from theater.models import (
    WorkspaceRecord,
    WorkspaceState,
)


class WorkspaceRegistration(WorkspaceHost):
    async def register(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        replay = self._operations.replay_idempotent_write(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.workspaces.register",
            params=params,
        )
        if replay is not None:
            assert isinstance(replay.value, Mapping)
            return replay.value
        facts = await workers.to_thread(
            inspect_existing_path,
            str(params["path"]),
            label="workspace.register.inspect",
        )
        self._validate_registration_facts(params, facts)

        def action(unit: WriteUnit) -> object:
            existing = self._store.workspaces.get_active_by_path(
                facts.path, connection=unit.connection
            )
            if existing is not None:
                self._validate_reuse(existing, params)
                return self.project(existing, connection=unit.connection)
            timestamp = self._clock()
            record = WorkspaceRecord(
                workspace_id=self._id_factory(),
                ownership_kind=str(params["ownership_kind"]),
                owner_id=str(params["owner_id"]),
                path=facts.path,
                canonical_repository_root=facts.canonical_repository_root,
                branch=facts.branch,
                resolved_base_commit=facts.head_commit,
                state=WorkspaceState.ACTIVE.value,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self._store.workspaces.create(record, connection=unit.connection)
            self._journal.append_workspace(unit, record)
            return self.project(record, connection=unit.connection)

        result = self._operations.execute_idempotent(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.workspaces.register",
            params=params,
            action=action,
        ).value
        assert isinstance(result, Mapping)
        return result

    def get(self, workspace_id: str, *, connection: Connection | None = None) -> WorkspaceRecord:
        record = self._store.workspaces.get(workspace_id, connection=connection)
        if record is None:
            raise WorkspaceNotFound(workspace_id)
        return record

    def list(
        self, *, cursor: str | None, limit: int, state: str | None
    ) -> tuple[tuple[WorkspaceRecord, ...], str | None]:
        if not 1 <= limit <= 500:
            raise ValueError("workspace page limit must be between 1 and 500")
        try:
            return self._store.workspaces.list_page(cursor=cursor, limit=limit, state=state)
        except KeyError as exc:
            raise ValueError(f"unknown workspace cursor {cursor!r}") from exc

    def project(
        self, record: WorkspaceRecord, *, connection: Connection | None = None
    ) -> dict[str, object]:
        return self._journal.project(record, connection=connection)
