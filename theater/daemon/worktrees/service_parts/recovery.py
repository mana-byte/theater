"""Retained workspace and interrupted-cleanup recovery."""

from __future__ import annotations

from theater.daemon import workers
from theater.daemon.worktrees import cleanup
from theater.daemon.worktrees.cleanup import (
    ExactCleanupResult,
)
from theater.daemon.worktrees.identity import (
    GitFactsError,
)
from theater.daemon.worktrees.service_parts._host import WorkspaceHost
from theater.models import (
    BadRequest,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
)


class WorkspaceRecovery(WorkspaceHost):
    async def reconcile_retained_workspaces(self) -> tuple[str, ...]:
        """Bounded read-only Git reconciliation for durable pending rows."""
        reconciled: list[str] = []
        cursor: tuple[float, str] | None = None
        while True:
            records, cursor = self._store.workspaces.list_by_states_page(
                (WorkspaceState.CREATING.value, WorkspaceState.RECONCILE.value),
                cursor=cursor,
                limit=100,
            )
            for record in records:
                if record.ownership_kind != WorkspaceOwnershipKind.THEATER.value:
                    continue
                if record.creation_operation_id is not None and record.cleanup_result is None:
                    if await self._reconcile_creation_intent(record.workspace_id):
                        reconciled.append(record.workspace_id)
                    continue
                if record.name is not None and await self._reconcile_named_workspace(record):
                    reconciled.append(record.workspace_id)
            if cursor is None:
                break
        return tuple(reconciled)

    async def recover_cleanup_deletion(
        self, workspace_id: str, *, operation_id: str, non_dispatch_proven: bool
    ) -> str | None:
        """Settle a stranded Theater cleanup without replaying Git deletion."""
        record = self.get(workspace_id)
        if (
            record.ownership_kind != WorkspaceOwnershipKind.THEATER.value
            or record.state != WorkspaceState.DELETING.value
            or record.deletion_operation_id != operation_id
            or record.deletion_token is None
        ):
            return None
        result: ExactCleanupResult | None = None
        if non_dispatch_proven:
            state = record.deletion_prior_state or WorkspaceState.ACTIVE.value
        else:
            if record.cleanup_delete_branch is None:
                return None
            try:
                result = await workers.to_thread(
                    cleanup.inspect_cleanup_result,
                    record,
                    delete_branch=record.cleanup_delete_branch,
                    label="workspace.cleanup.reconcile",
                )
            except GitFactsError as exc:
                result = ExactCleanupResult(
                    False,
                    False,
                    True,
                    (str(exc),),
                    uncertain=True,
                )
            state = (
                WorkspaceState.REMOVED.value
                if result.worktree_removed
                else WorkspaceState.RECONCILE.value
            )
        with self._store.write_unit() as unit:
            current = self.get(workspace_id, connection=unit.connection)
            if (
                current.state != WorkspaceState.DELETING.value
                or current.deletion_operation_id != operation_id
                or current.deletion_token != record.deletion_token
            ):
                return None
            if not self._store.workspaces.finish_delete(
                workspace_id,
                operation_id=operation_id,
                token=record.deletion_token,
                state=state,
                cleanup_result=None if result is None else result.to_wire(),
                updated_at=self._clock(),
                connection=unit.connection,
            ):
                return None
            updated = self.get(workspace_id, connection=unit.connection)
            self._journal.append_workspace(unit, updated)
        return state

    async def _reconcile_named_workspace(self, record: WorkspaceRecord) -> bool:
        try:
            inspection = await workers.to_thread(
                self._verify_named_workspace,
                record,
                label="workspace.named.reconcile",
            )
        except (BadRequest, GitFactsError):
            return False
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            changed = self._store.workspaces.settle_reconcile_workspace(
                record.workspace_id,
                state=WorkspaceState.ACTIVE.value,
                resolved_base_commit=inspection.head_commit,
                updated_at=timestamp,
                connection=unit.connection,
            )
            if changed:
                updated = self.get(record.workspace_id, connection=unit.connection)
                self._journal.append_workspace(unit, updated)
        return changed
