"""Worktree materialization and creation rollback."""

from __future__ import annotations

import asyncio

from theater.daemon import workers
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.worktrees import identity
from theater.daemon.worktrees.cleanup import (
    ExactCleanupResult,
    cleanup_exact_worktree,
)
from theater.daemon.worktrees.identity import (
    CreationIntentState,
    GitFactsError,
    inspect_registered_worktree,
)
from theater.daemon.worktrees.service_parts._common import (
    WorkspaceDeleting,
    WorkspaceReservation,
)
from theater.daemon.worktrees.service_parts._host import WorkspaceHost
from theater.models import (
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
)


class WorkspaceCreation(WorkspaceHost):
    async def materialize_creation(
        self, reservation: WorkspaceReservation, *, reservation_id: str
    ) -> WorkspaceReservation:
        """Create exactly one persisted Theater-owned intent, never recreate it."""
        workspace = self.get(reservation.workspace.workspace_id)
        if workspace.state == WorkspaceState.ACTIVE.value:
            return WorkspaceReservation(workspace, reservation.usage, reservation.created)
        if (
            workspace.state != WorkspaceState.CREATING.value
            or workspace.ownership_kind != WorkspaceOwnershipKind.THEATER.value
            or workspace.creation_operation_id != reservation_id
        ):
            raise WorkspaceDeleting(workspace.workspace_id, workspace.state)
        creation_worker = self._start_creation_worker(workspace)
        try:
            created_path = await asyncio.shield(creation_worker)
            self._verify_created_path(created_path, workspace.path)
            inspection = await workers.to_thread(
                inspect_registered_worktree,
                workspace,
                label="workspace.creation.verify",
            )
            self._verify_created_base(inspection.head_commit, workspace.resolved_base_commit)
        except asyncio.CancelledError:
            # Cancelling this coroutine does not stop the worker's Git process.
            # Retain the intent until a later exact inspection can observe it.
            self._mark_creation_reconcile_without_inspection(workspace.workspace_id)
            raise
        except BaseException:
            # A Git command can fail after a side effect.  Leave a reconcile
            # record; startup may inspect exact facts but must never retry it.
            await self._reconcile_creation_intent(workspace.workspace_id)
            raise
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            if not self._store.workspaces.mark_creation_ready(
                workspace.workspace_id,
                operation_id=reservation_id,
                updated_at=timestamp,
                connection=unit.connection,
            ):
                current = self.get(workspace.workspace_id, connection=unit.connection)
                raise WorkspaceDeleting(current.workspace_id, current.state)
            ready = self.get(workspace.workspace_id, connection=unit.connection)
            self._journal.append_workspace(unit, ready)
        return WorkspaceReservation(ready, reservation.usage, reservation.created)

    def _start_creation_worker(self, workspace: WorkspaceRecord) -> asyncio.Task[str]:
        task: asyncio.Task[str] = asyncio.create_task(
            workers.to_thread(
                self._create_from_intent,
                workspace,
                label="workspace.creation.materialize",
            )
        )
        self._creation_workers[workspace.workspace_id] = (
            self._creation_workers.get(workspace.workspace_id, 0) + 1
        )
        task.add_done_callback(
            lambda _completed: self._finish_creation_worker(workspace.workspace_id)
        )
        return task

    def _finish_creation_worker(self, workspace_id: str) -> None:
        remaining = self._creation_workers.get(workspace_id, 0) - 1
        if remaining > 0:
            self._creation_workers[workspace_id] = remaining
        else:
            self._creation_workers.pop(workspace_id, None)

    async def rollback_created_reservation(self, *, workspace_id: str, reservation_id: str) -> bool:
        """Remove only an exact, fresh Theater workspace after no dispatch occurred."""
        prepared = self._begin_creation_rollback(
            workspace_id=workspace_id, reservation_id=reservation_id
        )
        if prepared is None:
            return False
        workspace, token = prepared
        result = await workers.to_thread(
            self._cleanup_creation_rollback,
            workspace,
            label="workspace.creation.rollback",
        )
        return self._finish_creation_rollback(
            workspace=workspace,
            reservation_id=reservation_id,
            token=token,
            result=result,
        )

    async def rollback_created_reservation_after_recovery(
        self, *, workspace_id: str, reservation_id: str
    ) -> bool:
        """Recover a proven-undispatched launch without blocking the daemon loop."""
        current = self.get(workspace_id)
        if (
            current.ownership_kind == WorkspaceOwnershipKind.THEATER.value
            and current.creation_operation_id == reservation_id
            and current.state == WorkspaceState.DELETING.value
            and current.deletion_operation_id == reservation_id
            and current.deletion_token is not None
            and current.cleanup_force is True
            and current.cleanup_delete_branch is True
            and current.cleanup_force_branch is True
        ):
            result = await workers.to_thread(
                self._cleanup_creation_rollback,
                current,
                label="workspace.creation.rollback.recovery",
            )
            return self._finish_creation_rollback(
                workspace=current,
                reservation_id=reservation_id,
                token=current.deletion_token,
                result=result,
            )
        return await self.rollback_created_reservation(
            workspace_id=workspace_id, reservation_id=reservation_id
        )

    def begin_creation_rollback_in_unit(
        self,
        *,
        workspace_id: str,
        reservation_id: str,
        timestamp: float,
        unit: WriteUnit,
    ) -> tuple[WorkspaceRecord, str] | None:
        """Persist the exact rollback fence inside a caller-owned write unit."""
        current = self.get(workspace_id, connection=unit.connection)
        if (
            current.ownership_kind != WorkspaceOwnershipKind.THEATER.value
            or current.creation_operation_id != reservation_id
            or current.state != WorkspaceState.ACTIVE.value
            or self._store.workspaces.active_usages(
                current.workspace_id, connection=unit.connection
            )
        ):
            return None
        token = f"rollback-{self._id_factory()}"
        self._begin_delete(
            current,
            request_id=reservation_id,
            token=token,
            force=True,
            delete_branch=True,
            force_branch=True,
            timestamp=timestamp,
            connection=unit.connection,
            allowed_states=(WorkspaceState.ACTIVE.value,),
        )
        deleting = self.get(current.workspace_id, connection=unit.connection)
        self._journal.append_workspace(unit, deleting)
        return current, token

    def _begin_creation_rollback(
        self, *, workspace_id: str, reservation_id: str
    ) -> tuple[WorkspaceRecord, str] | None:
        with self._store.write_unit() as unit:
            return self.begin_creation_rollback_in_unit(
                workspace_id=workspace_id,
                reservation_id=reservation_id,
                timestamp=self._clock(),
                unit=unit,
            )

    @staticmethod
    def _cleanup_creation_rollback(workspace: WorkspaceRecord) -> ExactCleanupResult:
        try:
            return cleanup_exact_worktree(
                workspace, force=True, delete_branch=True, force_branch=True
            )
        except GitFactsError as exc:
            return ExactCleanupResult(False, False, True, (str(exc),))

    def _finish_creation_rollback(
        self,
        *,
        workspace: WorkspaceRecord,
        reservation_id: str,
        token: str,
        result: ExactCleanupResult,
    ) -> bool:
        with self._store.write_unit() as unit:
            current = self.get(workspace.workspace_id, connection=unit.connection)
            if (
                current.state != WorkspaceState.DELETING.value
                or current.deletion_operation_id != reservation_id
                or current.deletion_token != token
            ):
                return False
            state = (
                WorkspaceState.REMOVED.value
                if result.worktree_removed
                else WorkspaceState.RECONCILE.value
            )
            if not self._store.workspaces.finish_delete(
                workspace.workspace_id,
                operation_id=reservation_id,
                token=token,
                state=state,
                cleanup_result=result.to_wire(),
                updated_at=self._clock(),
                connection=unit.connection,
            ):
                return False
            updated = self.get(workspace.workspace_id, connection=unit.connection)
            self._journal.append_workspace(unit, updated)
        return result.worktree_removed

    async def _reconcile_creation_intent(self, workspace_id: str) -> bool:
        record = self.get(workspace_id)
        if (
            record.state
            not in {
                WorkspaceState.CREATING.value,
                WorkspaceState.RECONCILE.value,
            }
            or record.creation_operation_id is None
        ):
            return record.state == WorkspaceState.ACTIVE.value
        inspection = await workers.to_thread(
            identity.inspect_creation_intent,
            record,
            label="workspace.creation.reconcile",
        )
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            if (
                inspection.state is CreationIntentState.READY
                and record.state == WorkspaceState.CREATING.value
            ):
                changed = self._store.workspaces.mark_creation_ready(
                    record.workspace_id,
                    operation_id=record.creation_operation_id,
                    updated_at=timestamp,
                    connection=unit.connection,
                )
            elif (
                inspection.state is CreationIntentState.READY
                and record.state == WorkspaceState.RECONCILE.value
            ):
                changed = self._store.workspaces.mark_reconciled_creation_ready(
                    record.workspace_id,
                    operation_id=record.creation_operation_id,
                    updated_at=timestamp,
                    connection=unit.connection,
                )
            elif (
                inspection.state is CreationIntentState.ABSENT
                and record.state == WorkspaceState.CREATING.value
            ):
                changed = self._store.workspaces.mark_creation_removed(
                    record.workspace_id,
                    operation_id=record.creation_operation_id,
                    updated_at=timestamp,
                    connection=unit.connection,
                )
            elif record.state == WorkspaceState.CREATING.value:
                changed = self._store.workspaces.mark_creation_reconcile(
                    record.workspace_id,
                    operation_id=record.creation_operation_id,
                    updated_at=timestamp,
                    connection=unit.connection,
                )
            else:
                changed = False
            if (
                changed
                and inspection.state is CreationIntentState.ABSENT
                and record.state == WorkspaceState.CREATING.value
            ):
                self._release_retired_creation_usage(
                    unit,
                    workspace_id=record.workspace_id,
                    operation_id=record.creation_operation_id,
                    timestamp=timestamp,
                )
            if changed:
                updated = self.get(record.workspace_id, connection=unit.connection)
                self._journal.append_workspace(unit, updated)
        return changed

    def _mark_creation_reconcile_without_inspection(self, workspace_id: str) -> bool:
        """Fence a cancelled Git worker without claiming that it did nothing."""
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            record = self.get(workspace_id, connection=unit.connection)
            if (
                record.state != WorkspaceState.CREATING.value
                or record.creation_operation_id is None
            ):
                return False
            changed = self._store.workspaces.mark_creation_reconcile(
                record.workspace_id,
                operation_id=record.creation_operation_id,
                updated_at=timestamp,
                connection=unit.connection,
            )
            if changed:
                updated = self.get(record.workspace_id, connection=unit.connection)
                self._journal.append_workspace(unit, updated)
        return changed

    def _release_retired_creation_usage(
        self,
        unit: WriteUnit,
        *,
        workspace_id: str,
        operation_id: str,
        timestamp: float,
    ) -> None:
        for usage in self._store.workspaces.active_usages(workspace_id, connection=unit.connection):
            if (
                usage.holder_kind != WorkspaceUsageHolderKind.RESERVATION.value
                or usage.holder_id != operation_id
            ):
                continue
            if self._store.workspaces.release_usage(
                usage.usage_id,
                released_at=timestamp,
                reason="creation_absent_after_restart",
                connection=unit.connection,
            ):
                self._journal.append_usage(
                    unit,
                    WorkspaceUsageRecord(
                        usage_id=usage.usage_id,
                        workspace_id=usage.workspace_id,
                        holder_kind=usage.holder_kind,
                        holder_id=usage.holder_id,
                        acquired_at=usage.acquired_at,
                        released_at=timestamp,
                        release_reason="creation_absent_after_restart",
                    ),
                )
