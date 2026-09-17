"""Durable workspace lifecycle, usage fences, and explicit cleanup."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import Connection

from theater.daemon import workers
from theater.daemon.operations import (
    DispatchIntent,
    OperationOutcome,
    OperationService,
    PreparedOperation,
)
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.worktrees.cleanup import (
    ExactCleanupResult,
    cleanup_exact_worktree,
    cleanup_retained_branch,
)
from theater.daemon.worktrees.identity import (
    ExistingPathFacts,
    GitFactsError,
    inspect_existing_path,
    resolve_creation_facts,
)
from theater.daemon.worktrees.named import (
    create_named_worktree,
    remove_named_worktree,
    verify_named_worktree,
)
from theater.daemon.worktrees.paths import branch_name
from theater.daemon.worktrees.unique import create_worktree, remove_worktree
from theater.models import (
    BadRequest,
    JournalEventRecord,
    PublicOperationRecord,
    TheaterError,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    new_id,
    now,
)


class WorkspaceNotFound(TheaterError):
    code = "not_found"

    def __init__(self, workspace_id: str) -> None:
        self.details = {"workspace_id": workspace_id}
        super().__init__(f"no workspace {workspace_id!r} exists")


class WorkspaceInUse(TheaterError):
    code = "workspace_in_use"

    def __init__(self, workspace_id: str, usage_ids: tuple[str, ...]) -> None:
        self.details = {"workspace_id": workspace_id, "usage_ids": list(usage_ids)}
        super().__init__(f"workspace {workspace_id!r} has active usage and cannot be deleted")


class WorkspaceDeleting(TheaterError):
    code = "workspace_deleting"

    def __init__(self, workspace_id: str, state: str) -> None:
        self.details = {"workspace_id": workspace_id, "state": state}
        super().__init__(f"workspace {workspace_id!r} is {state} and cannot admit new usage")


class WorkspaceOwnershipConflict(TheaterError):
    code = "ownership_conflict"

    def __init__(self, workspace_id: str, reason: str) -> None:
        self.details = {"workspace_id": workspace_id, "reason": reason}
        super().__init__(f"workspace {workspace_id!r} ownership check failed: {reason}")


@dataclass(frozen=True, slots=True)
class WorkspaceRequest:
    workspace_id: str | None = None
    cwd: str | None = None
    worktree: bool | str = False
    base_ref: str | None = None


@dataclass(frozen=True, slots=True)
class WorkspaceReservation:
    workspace: WorkspaceRecord
    usage: WorkspaceUsageRecord
    created: bool


class WorkspaceService:
    def __init__(
        self,
        store,
        operations: OperationService,
        *,
        clock: Callable[[], float] = now,
        id_factory: Callable[[], str] = new_id,
    ) -> None:
        self._store = store
        self._operations = operations
        self._clock = clock
        self._id_factory = id_factory

    async def register(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
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
            self._append_workspace_event(unit, record)
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
        usages = self._store.workspaces.active_usages(record.workspace_id, connection=connection)
        fence = None
        if record.deletion_token is not None:
            fence = {
                "token": record.deletion_token,
                "revision": self._token_revision(record.deletion_token),
                "owner_id": record.owner_id,
                "operation_id": record.deletion_operation_id,
            }
        return {
            "workspace_id": record.workspace_id,
            "ownership_kind": record.ownership_kind,
            "owner_id": record.owner_id,
            "path": record.path,
            "canonical_repository_root": record.canonical_repository_root,
            "resolved_base_commit": record.resolved_base_commit,
            "branch": record.branch,
            "name": record.name,
            "state": record.state,
            "usages": [self._usage_to_wire(usage) for usage in usages],
            "deletion_fence": fence,
        }

    async def reserve(
        self,
        request: WorkspaceRequest,
        *,
        reservation_id: str,
        owner_id: str = "local_operator",
    ) -> WorkspaceReservation:
        self._validate_request(request)
        if request.workspace_id is not None:
            return self._reserve_existing(request.workspace_id, reservation_id)
        assert request.cwd is not None
        if request.worktree is True:
            return await self._reserve_unique(request, reservation_id, owner_id)
        if isinstance(request.worktree, str):
            return await self._reserve_named(request, reservation_id, owner_id)
        facts = await workers.to_thread(
            inspect_existing_path,
            request.cwd,
            label="workspace.reserve.inspect",
        )
        return self._reserve_borrowed(facts, reservation_id, owner_id)

    def handoff_usage(
        self, reservation_usage_id: str, *, participant_id: str
    ) -> WorkspaceUsageRecord:
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            reservation = self._store.workspaces.get_usage(
                reservation_usage_id, connection=unit.connection
            )
            if reservation is None or reservation.holder_kind != "reservation":
                raise KeyError(f"reservation usage {reservation_usage_id!r} was not found")
            workspace = self.get(reservation.workspace_id, connection=unit.connection)
            self._require_active(workspace)
            existing = self._store.workspaces.get_active_usage(
                workspace.workspace_id,
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                connection=unit.connection,
            )
            if existing is not None and reservation.released_at is not None:
                return existing
            usage = WorkspaceUsageRecord(
                usage_id=self._id_factory(),
                workspace_id=workspace.workspace_id,
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                acquired_at=timestamp,
            )
            self._store.workspaces.handoff_usage(
                reservation_usage_id=reservation_usage_id,
                participant_usage=usage,
                handed_off_at=timestamp,
                connection=unit.connection,
            )
            self._append_handoff_event(unit, reservation, usage)
        return usage

    def release_usage(self, usage_id: str, *, reason: str) -> WorkspaceUsageRecord:
        if not reason or len(reason) > 512:
            raise ValueError("workspace release reason must contain 1 to 512 characters")
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            usage = self._store.workspaces.get_usage(usage_id, connection=unit.connection)
            if usage is None:
                raise KeyError(f"workspace usage {usage_id!r} was not found")
            if usage.released_at is not None:
                return usage
            if not self._store.workspaces.release_usage(
                usage_id,
                released_at=timestamp,
                reason=reason,
                connection=unit.connection,
            ):
                raise RuntimeError("workspace usage changed during its write unit")
            released = WorkspaceUsageRecord(
                usage_id=usage.usage_id,
                workspace_id=usage.workspace_id,
                holder_kind=usage.holder_kind,
                holder_id=usage.holder_id,
                acquired_at=usage.acquired_at,
                released_at=timestamp,
                release_reason=reason,
            )
            self._append_usage_event(unit, released, action="released")
        return released

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
                timestamp=self._clock(),
                connection=unit.connection,
            )
            deleting = self.get(workspace.workspace_id, connection=unit.connection)
            self._append_workspace_event(unit, deleting, revision=revision)
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
                timestamp=self._clock(),
                connection=unit.connection,
                allowed_states=(workspace.state,),
            )
            deleting = self.get(workspace.workspace_id, connection=unit.connection)
            self._append_workspace_event(unit, deleting, revision=revision)
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

    def _reserve_existing(self, workspace_id: str, reservation_id: str) -> WorkspaceReservation:
        with self._store.write_unit() as unit:
            workspace = self.get(workspace_id, connection=unit.connection)
            usage, created = self._acquire_reservation(
                unit, workspace, reservation_id=reservation_id
            )
            if created:
                self._append_usage_event(unit, usage, action="acquired")
        return WorkspaceReservation(workspace, usage, False)

    def _reserve_borrowed(
        self, facts: ExistingPathFacts, reservation_id: str, owner_id: str
    ) -> WorkspaceReservation:
        with self._store.write_unit() as unit:
            workspace = self._store.workspaces.get_active_by_path(
                facts.path, connection=unit.connection
            )
            created_workspace = False
            if workspace is None:
                timestamp = self._clock()
                workspace = WorkspaceRecord(
                    workspace_id=self._id_factory(),
                    ownership_kind=WorkspaceOwnershipKind.BORROWED.value,
                    owner_id=owner_id,
                    path=facts.path,
                    canonical_repository_root=facts.canonical_repository_root,
                    branch=facts.branch,
                    resolved_base_commit=facts.head_commit,
                    state=WorkspaceState.ACTIVE.value,
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                self._store.workspaces.create(workspace, connection=unit.connection)
                created_workspace = True
            elif workspace.ownership_kind != WorkspaceOwnershipKind.BORROWED.value:
                raise WorkspaceOwnershipConflict(workspace.workspace_id, "path_already_owned")
            usage, created_usage = self._acquire_reservation(
                unit, workspace, reservation_id=reservation_id
            )
            self._append_creation_events(
                unit,
                workspace=workspace if created_workspace else None,
                usage=usage if created_usage else None,
            )
        return WorkspaceReservation(workspace, usage, created_workspace)

    async def _reserve_unique(
        self, request: WorkspaceRequest, reservation_id: str, owner_id: str
    ) -> WorkspaceReservation:
        assert request.cwd is not None
        facts = await workers.to_thread(
            resolve_creation_facts,
            request.cwd,
            request.base_ref,
            label="workspace.unique.resolve",
        )
        workspace_id = self._id_factory()
        path = await workers.to_thread(
            create_worktree,
            repo_root=facts.canonical_repository_root,
            child_id=workspace_id,
            base_branch=facts.resolved_base_commit,
            label="workspace.unique.create",
        )
        record = WorkspaceRecord(
            workspace_id=workspace_id,
            ownership_kind=WorkspaceOwnershipKind.THEATER.value,
            owner_id=owner_id,
            path=path,
            canonical_repository_root=facts.canonical_repository_root,
            branch=branch_name(workspace_id),
            resolved_base_commit=facts.resolved_base_commit,
            state=WorkspaceState.ACTIVE.value,
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        try:
            return self._persist_created(record, reservation_id)
        except BaseException:
            await workers.to_thread(
                remove_worktree,
                repo_root=facts.canonical_repository_root,
                child_id=workspace_id,
                delete_branch=True,
                label="workspace.unique.rollback",
            )
            raise

    async def _reserve_named(
        self, request: WorkspaceRequest, reservation_id: str, owner_id: str
    ) -> WorkspaceReservation:
        assert request.cwd is not None and isinstance(request.worktree, str)
        name = request.worktree
        initiating = await workers.to_thread(
            resolve_creation_facts,
            request.cwd,
            request.base_ref,
            label="workspace.named.resolve",
        )
        existing = self._store.workspaces.get_active_named(
            initiating.canonical_repository_root, name
        )
        if existing is not None:
            self._require_active(existing)
            if (
                request.base_ref is not None
                and existing.resolved_base_commit != initiating.resolved_base_commit
            ):
                raise WorkspaceOwnershipConflict(existing.workspace_id, "named_base_mismatch")
            await workers.to_thread(
                verify_named_worktree,
                repo_root=initiating.canonical_repository_root,
                name=name,
                expected_path=existing.path,
                expected_branch=existing.branch or "",
                label="workspace.named.verify",
            )
            return self._reserve_existing(existing.workspace_id, reservation_id)
        workspace_id = self._id_factory()
        path, branch = await workers.to_thread(
            create_named_worktree,
            repo_root=initiating.canonical_repository_root,
            name=name,
            base_branch=initiating.resolved_base_commit,
            label="workspace.named.create",
        )
        record = WorkspaceRecord(
            workspace_id=workspace_id,
            ownership_kind=WorkspaceOwnershipKind.THEATER.value,
            owner_id=owner_id,
            path=path,
            canonical_repository_root=initiating.canonical_repository_root,
            branch=branch,
            resolved_base_commit=initiating.resolved_base_commit,
            name=name,
            state=WorkspaceState.ACTIVE.value,
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        try:
            return self._persist_created(record, reservation_id)
        except BaseException:
            await workers.to_thread(
                remove_named_worktree,
                repo_root=initiating.canonical_repository_root,
                name=name,
                delete_branch=True,
                label="workspace.named.rollback",
            )
            raise

    def _persist_created(
        self, record: WorkspaceRecord, reservation_id: str
    ) -> WorkspaceReservation:
        with self._store.write_unit() as unit:
            self._store.workspaces.create(record, connection=unit.connection)
            usage, created_usage = self._acquire_reservation(
                unit, record, reservation_id=reservation_id
            )
            assert created_usage
            self._append_creation_events(unit, workspace=record, usage=usage)
        return WorkspaceReservation(record, usage, True)

    def _acquire_reservation(
        self, unit: WriteUnit, workspace: WorkspaceRecord, *, reservation_id: str
    ) -> tuple[WorkspaceUsageRecord, bool]:
        self._require_active(workspace)
        existing = self._store.workspaces.get_active_usage(
            workspace.workspace_id,
            holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
            holder_id=reservation_id,
            connection=unit.connection,
        )
        if existing is not None:
            return existing, False
        usage = WorkspaceUsageRecord(
            usage_id=self._id_factory(),
            workspace_id=workspace.workspace_id,
            holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
            holder_id=reservation_id,
            acquired_at=self._clock(),
        )
        if not self._store.workspaces.acquire_usage(usage, connection=unit.connection):
            current = self.get(workspace.workspace_id, connection=unit.connection)
            raise WorkspaceDeleting(current.workspace_id, current.state)
        return usage, True

    def _begin_delete(
        self,
        workspace: WorkspaceRecord,
        *,
        request_id: str,
        token: str,
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
            self._append_workspace_event(unit, updated)
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
            if workspace.state == WorkspaceState.REMOVED.value:
                result = await workers.to_thread(
                    cleanup_retained_branch,
                    workspace,
                    delete_branch=delete_branch,
                    force_branch=force_branch,
                    label="workspace.cleanup.branch",
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
                updated_at=self._clock(),
                connection=unit.connection,
            ):
                raise RuntimeError("workspace cleanup fence changed after Git execution")
            updated = self.get(workspace.workspace_id, connection=unit.connection)
            self._append_workspace_event(unit, updated)
            value = {
                **result.to_wire(),
                "workspace": self.project(updated, connection=unit.connection),
            }
        if result.worktree_removed:
            phase = "workspace_cleanup_partial" if result.errors else "workspace_cleanup_succeeded"
            if result.uncertain:
                return OperationOutcome.uncertain(
                    phase="workspace_cleanup_uncertain",
                    error={
                        "code": "internal",
                        "message": "Git cleanup may have executed; inspect exact workspace facts",
                        "details": value,
                    },
                )
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

    def _append_workspace_event(
        self, unit: WriteUnit, record: WorkspaceRecord, *, revision: int | None = None
    ) -> None:
        value = revision or self._store.journal.current_sequence(connection=unit.connection) + 1
        self._store.journal.append_group(
            unit,
            [
                JournalEventRecord(
                    kind="workspace.updated",
                    entity_id=record.workspace_id,
                    entity_revision=value,
                    payload=self.project(record, connection=unit.connection),
                    recorded_at=record.updated_at,
                )
            ],
        )

    def _append_usage_event(
        self, unit: WriteUnit, usage: WorkspaceUsageRecord, *, action: str
    ) -> None:
        revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        self._store.journal.append_group(
            unit,
            [
                JournalEventRecord(
                    kind="workspace.usage_changed",
                    entity_id=usage.workspace_id,
                    entity_revision=revision,
                    payload={"action": action, "usage": self._usage_to_wire(usage)},
                    recorded_at=(
                        usage.released_at if usage.released_at is not None else usage.acquired_at
                    ),
                )
            ],
        )

    def _append_handoff_event(
        self,
        unit: WriteUnit,
        reservation: WorkspaceUsageRecord,
        participant: WorkspaceUsageRecord,
    ) -> None:
        revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        self._store.journal.append_group(
            unit,
            [
                JournalEventRecord(
                    kind="workspace.usage_changed",
                    entity_id=participant.workspace_id,
                    entity_revision=revision,
                    payload={
                        "action": "handoff",
                        "handoff": {
                            "workspace_id": participant.workspace_id,
                            "reservation_id": reservation.holder_id,
                            "participant_id": participant.holder_id,
                        },
                        "usage": self._usage_to_wire(participant),
                    },
                    recorded_at=participant.acquired_at,
                )
            ],
        )

    def _append_creation_events(
        self,
        unit: WriteUnit,
        *,
        workspace: WorkspaceRecord | None,
        usage: WorkspaceUsageRecord | None,
    ) -> None:
        if workspace is None and usage is None:
            return
        revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        events: list[JournalEventRecord] = []
        if workspace is not None:
            events.append(
                JournalEventRecord(
                    kind="workspace.updated",
                    entity_id=workspace.workspace_id,
                    entity_revision=revision,
                    payload=self.project(workspace, connection=unit.connection),
                    recorded_at=workspace.updated_at,
                )
            )
            revision += 1
        if usage is not None:
            events.append(
                JournalEventRecord(
                    kind="workspace.usage_changed",
                    entity_id=usage.workspace_id,
                    entity_revision=revision,
                    payload={"action": "acquired", "usage": self._usage_to_wire(usage)},
                    recorded_at=usage.acquired_at,
                )
            )
        self._store.journal.append_group(unit, events)

    @staticmethod
    def _usage_to_wire(usage: WorkspaceUsageRecord) -> dict[str, object]:
        return {
            "usage_id": usage.usage_id,
            "workspace_id": usage.workspace_id,
            "holder_kind": usage.holder_kind,
            "holder_id": usage.holder_id,
            "acquired_at": usage.acquired_at,
            "released_at": usage.released_at,
            "release_reason": usage.release_reason,
        }

    @staticmethod
    def _validate_registration_facts(
        params: Mapping[str, object], facts: ExistingPathFacts
    ) -> None:
        if params["ownership_kind"] not in {
            WorkspaceOwnershipKind.FRONTEND.value,
            WorkspaceOwnershipKind.BORROWED.value,
        }:
            raise WorkspaceOwnershipConflict("unregistered", "invalid_external_owner")
        expected = {
            "canonical_repository_root": facts.canonical_repository_root,
            "branch": facts.branch,
            "resolved_base_commit": facts.head_commit,
        }
        for key, value in expected.items():
            if key in params and params[key] != value:
                raise GitFactsError(f"supplied {key} does not match the existing directory")

    @staticmethod
    def _validate_reuse(record: WorkspaceRecord, params: Mapping[str, object]) -> None:
        if record.state != WorkspaceState.ACTIVE.value:
            raise WorkspaceDeleting(record.workspace_id, record.state)
        if (
            record.ownership_kind != params["ownership_kind"]
            or record.owner_id != params["owner_id"]
        ):
            raise WorkspaceOwnershipConflict(record.workspace_id, "path_already_owned")

    @staticmethod
    def _validate_request(request: WorkspaceRequest) -> None:
        if request.workspace_id is not None:
            if (
                request.cwd is not None
                or request.worktree is not False
                or request.base_ref is not None
            ):
                raise ValueError("workspace_id cannot be combined with cwd, worktree, or base_ref")
            return
        if request.cwd is None:
            raise ValueError("workspace preparation requires workspace_id or cwd")
        if not isinstance(request.worktree, (bool, str)) or request.worktree == "":
            raise ValueError("worktree must be false, true, or a non-empty name")
        if request.base_ref is not None and request.worktree is False:
            raise ValueError("base_ref requires unique or named worktree creation")

    @staticmethod
    def _require_active(workspace: WorkspaceRecord) -> None:
        if workspace.state != WorkspaceState.ACTIVE.value:
            raise WorkspaceDeleting(workspace.workspace_id, workspace.state)

    @staticmethod
    def _token_revision(token: str) -> int:
        try:
            revision = int(token.split("-", 1)[0])
        except ValueError:
            return 0
        return max(revision, 0)


__all__ = [
    "WorkspaceDeleting",
    "WorkspaceInUse",
    "WorkspaceNotFound",
    "WorkspaceOwnershipConflict",
    "WorkspaceRequest",
    "WorkspaceReservation",
    "WorkspaceService",
]
