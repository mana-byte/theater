"""Workspace deletion admission and cleanup side effects."""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import Connection

from theater.daemon import workers
from theater.daemon.operations import (
    DispatchIntent,
    OperationOutcome,
    PreparedOperation,
)
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.worktrees.cleanup import (
    ExactCleanupResult,
    cleanup_exact_worktree,
    cleanup_reconcile_creation,
    cleanup_retained_branch,
)
from theater.daemon.worktrees.identity import (
    GitFactsError,
)
from theater.daemon.worktrees.service_parts._common import (
    WorkspaceDeleting,
    WorkspaceInUse,
    WorkspaceOwnershipConflict,
)
from theater.daemon.worktrees.service_parts._host import WorkspaceHost
from theater.models import (
    BadRequest,
    PublicOperationRecord,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
)


class WorkspaceCleanup(WorkspaceHost):
    def prepare_external_delete(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        def action(unit: WriteUnit) -> object:
            workspace = self.get(str(params["workspace_id"]), connection=unit.connection)
            if workspace.ownership_kind == WorkspaceOwnershipKind.THEATER.value:
                raise WorkspaceOwnershipConflict(workspace.workspace_id, "theater_owned")
            request_id = self._id_factory()
            revision = self._store.journal.current_sequence(connection=unit.connection) + 1
            token = f"{revision}-{self._id_factory()}"
            self._begin_delete(
                workspace,
                request_id=request_id,
                token=token,
                force=None,
                delete_branch=None,
                force_branch=None,
                timestamp=self._clock(),
                connection=unit.connection,
            )
            deleting = self.get(workspace.workspace_id, connection=unit.connection)
            self._journal.append_workspace(unit, deleting, revision=revision)
            projected = self.project(deleting, connection=unit.connection)
            assert isinstance(projected["deletion_fence"], Mapping)
            return {"workspace": projected, **dict(projected["deletion_fence"])}

        result = self._operations.execute_idempotent(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.workspaces.prepare_delete",
            params=params,
            action=action,
        ).value
        assert isinstance(result, Mapping)
        return result

    def confirm_external_delete(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self._resolve_external_delete(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.workspaces.confirm_delete",
            params=params,
            state=WorkspaceState.REMOVED.value,
        )

    def cancel_external_delete(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        return self._resolve_external_delete(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.workspaces.cancel_delete",
            params=params,
            state=WorkspaceState.ACTIVE.value,
        )

    def cleanup(
        self,
        *,
        client_id: str,
        actor_participant_id: str | None,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        force = bool(params.get("force", False))
        delete_branch = bool(params.get("delete_branch", False))
        force_branch = bool(params.get("force_branch", False))
        if force_branch and not delete_branch:
            raise BadRequest("force_branch requires delete_branch=true")
        captured: dict[str, WorkspaceRecord] = {}

        def prepare(operation_id: str, unit: WriteUnit) -> PreparedOperation:
            workspace = self.get(str(params["workspace_id"]), connection=unit.connection)
            if workspace.ownership_kind != WorkspaceOwnershipKind.THEATER.value:
                raise WorkspaceOwnershipConflict(workspace.workspace_id, "not_theater_owned")
            if workspace.state == WorkspaceState.DELETING.value:
                raise WorkspaceDeleting(workspace.workspace_id, workspace.state)
            if workspace.state not in {
                WorkspaceState.ACTIVE.value,
                WorkspaceState.REMOVED.value,
                WorkspaceState.RECONCILE.value,
            }:
                raise WorkspaceDeleting(workspace.workspace_id, workspace.state)
            revision = self._store.journal.current_sequence(connection=unit.connection) + 1
            token = f"{revision}-{self._id_factory()}"
            self._begin_delete(
                workspace,
                request_id=operation_id,
                token=token,
                force=force,
                delete_branch=delete_branch,
                force_branch=force_branch,
                timestamp=self._clock(),
                connection=unit.connection,
                allowed_states=(workspace.state,),
            )
            deleting = self.get(workspace.workspace_id, connection=unit.connection)
            captured["workspace"] = workspace
            timestamp = self._clock()
            return PreparedOperation(
                record=PublicOperationRecord(
                    operation_id=operation_id,
                    kind="workspace_cleanup",
                    actor_client_id=client_id,
                    actor_participant_id=actor_participant_id,
                    target_ids=(workspace.workspace_id,),
                    state="accepted",
                    phase="workspace_cleanup_accepted",
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                response={"operation_id": operation_id, "state": "accepted"},
                events=(self._journal.workspace_event(unit, deleting, revision=revision),),
            )

        async def side_effect() -> OperationOutcome:
            workspace = captured["workspace"]
            return await self._cleanup_side_effect(
                workspace,
                force=force,
                delete_branch=delete_branch,
                force_branch=force_branch,
            )

        acceptance = self._operations.submit(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.workspaces.cleanup",
            params=params,
            prepare=prepare,
            dispatch=DispatchIntent(phase="workspace_cleanup_started"),
            side_effect=side_effect,
        )
        return acceptance.response

    def _begin_delete(
        self,
        workspace: WorkspaceRecord,
        *,
        request_id: str,
        token: str,
        force: bool | None,
        delete_branch: bool | None,
        force_branch: bool | None,
        timestamp: float,
        connection: Connection,
        allowed_states: tuple[str, ...] = (WorkspaceState.ACTIVE.value,),
    ) -> None:
        usages = self._store.workspaces.active_usages(workspace.workspace_id, connection=connection)
        if usages:
            raise WorkspaceInUse(workspace.workspace_id, tuple(item.usage_id for item in usages))
        if not self._store.workspaces.prepare_delete(
            workspace.workspace_id,
            operation_id=request_id,
            token=token,
            prior_state=workspace.state,
            cleanup_force=force,
            cleanup_delete_branch=delete_branch,
            cleanup_force_branch=force_branch,
            updated_at=timestamp,
            connection=connection,
            allowed_states=allowed_states,
        ):
            current = self.get(workspace.workspace_id, connection=connection)
            raise WorkspaceDeleting(current.workspace_id, current.state)

    def _resolve_external_delete(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        method: str,
        params: Mapping[str, object],
        state: str,
    ) -> Mapping[str, object]:
        def action(unit: WriteUnit) -> object:
            workspace = self.get(str(params["workspace_id"]), connection=unit.connection)
            if workspace.ownership_kind == WorkspaceOwnershipKind.THEATER.value:
                raise WorkspaceOwnershipConflict(workspace.workspace_id, "theater_owned")
            token = str(params["token"])
            if (
                workspace.state != WorkspaceState.DELETING.value
                or workspace.deletion_token != token
            ):
                raise WorkspaceOwnershipConflict(workspace.workspace_id, "deletion_token_mismatch")
            if not self._store.workspaces.resolve_external_delete(
                workspace.workspace_id,
                token=token,
                state=state,
                updated_at=self._clock(),
                connection=unit.connection,
            ):
                raise RuntimeError("workspace deletion fence changed during its write unit")
            updated = self.get(workspace.workspace_id, connection=unit.connection)
            self._journal.append_workspace(unit, updated)
            return {"workspace": self.project(updated, connection=unit.connection)}

        result = self._operations.execute_idempotent(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method=method,
            params=params,
            action=action,
        ).value
        assert isinstance(result, Mapping)
        return result

    async def _cleanup_side_effect(
        self,
        workspace: WorkspaceRecord,
        *,
        force: bool,
        delete_branch: bool,
        force_branch: bool,
    ) -> OperationOutcome:
        try:
            if workspace.workspace_id in self._creation_workers:
                result = ExactCleanupResult(
                    False,
                    False,
                    True,
                    ("workspace creation may still be executing; retry after reconciliation",),
                    uncertain=True,
                )
            elif workspace.state == WorkspaceState.REMOVED.value:
                result = await workers.to_thread(
                    cleanup_retained_branch,
                    workspace,
                    delete_branch=delete_branch,
                    force_branch=force_branch,
                    label="workspace.cleanup.branch",
                )
            elif (
                workspace.state == WorkspaceState.RECONCILE.value
                and workspace.creation_operation_id is not None
            ):
                result = await workers.to_thread(
                    cleanup_reconcile_creation,
                    workspace,
                    force=force,
                    delete_branch=delete_branch,
                    force_branch=force_branch,
                    label="workspace.cleanup.creation_partial",
                )
            else:
                result = await workers.to_thread(
                    cleanup_exact_worktree,
                    workspace,
                    force=force,
                    delete_branch=delete_branch,
                    force_branch=force_branch,
                    label="workspace.cleanup.worktree",
                )
        except GitFactsError as exc:
            result = ExactCleanupResult(False, False, True, (str(exc),))
        with self._store.write_unit() as unit:
            current = self.get(workspace.workspace_id, connection=unit.connection)
            operation_id = current.deletion_operation_id
            token = current.deletion_token
            if operation_id is None or token is None:
                raise RuntimeError("workspace cleanup fence disappeared after Git execution")
            state = (
                WorkspaceState.REMOVED.value
                if result.worktree_removed
                else WorkspaceState.RECONCILE.value
            )
            if not self._store.workspaces.finish_delete(
                workspace.workspace_id,
                operation_id=operation_id,
                token=token,
                state=state,
                cleanup_result=result.to_wire(),
                updated_at=self._clock(),
                connection=unit.connection,
            ):
                raise RuntimeError("workspace cleanup fence changed after Git execution")
            updated = self.get(workspace.workspace_id, connection=unit.connection)
            self._journal.append_workspace(unit, updated)
            value = {
                **result.to_wire(),
                "workspace": self.project(updated, connection=unit.connection),
            }
        if result.ok:
            phase = "workspace_cleanup_succeeded"
            return OperationOutcome.succeeded(phase=phase, result=value)
        if result.uncertain:
            return OperationOutcome.uncertain(
                phase="workspace_cleanup_uncertain",
                error={
                    "code": "internal",
                    "message": "Git cleanup may have executed; inspect exact workspace facts",
                    "details": value,
                },
            )
        return OperationOutcome.failed(
            phase="workspace_cleanup_failed",
            error={
                "code": "bad_request",
                "message": "workspace cleanup was refused or its identity could not be verified",
                "details": value,
            },
        )
