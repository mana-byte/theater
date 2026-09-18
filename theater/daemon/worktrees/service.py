"""Durable workspace lifecycle, usage fences, and explicit cleanup."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection

from theater.daemon import workers
from theater.daemon.events.publication import workspace_usage_event
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
    WorktreeCreationFacts,
    inspect_existing_path,
    inspect_registered_worktree,
    resolve_creation_facts,
    validate_canonical_repository,
)
from theater.daemon.worktrees.named import (
    create_named_worktree,
    verify_named_worktree,
)
from theater.daemon.worktrees.paths import (
    branch_name,
    named_branch_name,
    named_worktree_path,
    validate_name,
    worktree_path,
)
from theater.daemon.worktrees.unique import create_worktree
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
            facts = await workers.to_thread(
                resolve_creation_facts,
                request.cwd,
                request.base_ref,
                label="workspace.unique.resolve",
            )
            with self._store.write_unit() as unit:
                reservation = self._create_unique_intent(
                    request,
                    facts,
                    reservation_id=reservation_id,
                    owner_id=owner_id,
                    connection=unit.connection,
                )
            return await self.materialize_creation(reservation, reservation_id=reservation_id)
        if isinstance(request.worktree, str):
            facts = await workers.to_thread(
                resolve_creation_facts,
                request.cwd,
                request.base_ref,
                label="workspace.named.resolve",
            )
            with self._store.write_unit() as unit:
                reservation = self._create_named_intent_or_join(
                    request,
                    facts,
                    reservation_id=reservation_id,
                    owner_id=owner_id,
                    connection=unit.connection,
                )
            return await self.materialize_creation(reservation, reservation_id=reservation_id)
        facts = await workers.to_thread(
            inspect_existing_path,
            request.cwd,
            label="workspace.reserve.inspect",
        )
        return self._reserve_borrowed(facts, reservation_id, owner_id)

    def reserve_for_spawn(
        self,
        request: WorkspaceRequest,
        *,
        reservation_id: str,
        owner_id: str,
        connection: Connection,
    ) -> WorkspaceReservation:
        """Persist a launch's workspace identity before its detached work starts.

        Git fact resolution is deliberately synchronous here: this runs inside
        operation acceptance, before the operation can be observed or replayed.
        """
        self._validate_request(request)
        if request.workspace_id is not None:
            return self._reserve_existing_in_connection(
                request.workspace_id, reservation_id=reservation_id, connection=connection
            )
        assert request.cwd is not None
        if request.worktree is True or isinstance(request.worktree, str):
            creation_facts = resolve_creation_facts(request.cwd, request.base_ref)
            if request.worktree is True:
                return self._create_unique_intent(
                    request,
                    creation_facts,
                    reservation_id=reservation_id,
                    owner_id=owner_id,
                    connection=connection,
                )
            return self._create_named_intent_or_join(
                request,
                creation_facts,
                reservation_id=reservation_id,
                owner_id=owner_id,
                connection=connection,
            )
        existing_facts = inspect_existing_path(request.cwd)
        return self._reserve_borrowed_in_connection(
            existing_facts, reservation_id=reservation_id, owner_id=owner_id, connection=connection
        )

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
        try:
            created_path = await workers.to_thread(
                self._create_from_intent,
                workspace,
                label="workspace.creation.materialize",
            )
            self._verify_created_path(created_path, workspace.path)
            inspection = await workers.to_thread(
                inspect_registered_worktree,
                workspace,
                label="workspace.creation.verify",
            )
            self._verify_created_base(inspection.head_commit, workspace.resolved_base_commit)
        except BaseException:
            # A Git command can fail after a side effect.  Leave a reconcile
            # record; startup may inspect exact facts but must never retry it.
            self._reconcile_creation_intent(workspace.workspace_id)
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
            self._append_workspace_event(unit, ready)
        return WorkspaceReservation(ready, reservation.usage, reservation.created)

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

    def rollback_created_reservation_after_recovery(
        self, *, workspace_id: str, reservation_id: str
    ) -> bool:
        """Startup-only synchronous rollback after durable non-dispatch proof."""
        prepared = self._begin_creation_rollback(
            workspace_id=workspace_id, reservation_id=reservation_id
        )
        if prepared is None:
            return False
        workspace, token = prepared
        result = self._cleanup_creation_rollback(workspace)
        return self._finish_creation_rollback(
            workspace=workspace,
            reservation_id=reservation_id,
            token=token,
            result=result,
        )

    def _begin_creation_rollback(
        self, *, workspace_id: str, reservation_id: str
    ) -> tuple[WorkspaceRecord, str] | None:
        with self._store.write_unit() as unit:
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
                timestamp=self._clock(),
                connection=unit.connection,
                allowed_states=(WorkspaceState.ACTIVE.value,),
            )
            deleting = self.get(current.workspace_id, connection=unit.connection)
            self._append_workspace_event(unit, deleting)
        return current, token

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
                updated_at=self._clock(),
                connection=unit.connection,
            ):
                return False
            updated = self.get(workspace.workspace_id, connection=unit.connection)
            self._append_workspace_event(unit, updated)
        return result.worktree_removed

    def reconcile_retained_workspaces(self) -> tuple[str, ...]:
        """Bounded read-only Git reconciliation for durable pending rows."""
        records = self._store.workspaces.list_by_states(
            (WorkspaceState.CREATING.value, WorkspaceState.RECONCILE.value), limit=500
        )
        reconciled: list[str] = []
        for record in records:
            if record.ownership_kind != WorkspaceOwnershipKind.THEATER.value:
                continue
            if record.state == WorkspaceState.CREATING.value:
                if self._reconcile_creation_intent(record.workspace_id):
                    reconciled.append(record.workspace_id)
                continue
            if record.name is not None and self._reconcile_named_workspace(record):
                reconciled.append(record.workspace_id)
        return tuple(reconciled)

    def recover_cleanup_deletion(
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
        if non_dispatch_proven:
            state = WorkspaceState.ACTIVE.value
        else:
            state = self._cleanup_recovery_state(record)
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
                updated_at=self._clock(),
                connection=unit.connection,
            ):
                return None
            updated = self.get(workspace_id, connection=unit.connection)
            self._append_workspace_event(unit, updated)
        return state

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
            self._append_handoff_event(unit, usage)
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
            self._append_usage_event(unit, released)
        return released

    def release_participant_usage(
        self,
        *,
        workspace_id: str,
        participant_id: str,
        reason: str,
    ) -> WorkspaceUsageRecord | None:
        """Release a held participant usage only after its exit is confirmed."""
        with self._store.write_unit() as unit:
            usage = self._store.workspaces.get_active_usage(
                workspace_id,
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                connection=unit.connection,
            )
            if usage is None:
                return None
            timestamp = self._clock()
            if not self._store.workspaces.release_usage(
                usage.usage_id,
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
            self._append_usage_event(unit, released)
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
                events=(self._workspace_event(unit, deleting, revision=revision),),
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
            existing = self._store.workspaces.get_active_usage(
                workspace_id,
                holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
                holder_id=reservation_id,
                connection=unit.connection,
            )
            reservation = self._reserve_existing_in_connection(
                workspace_id, reservation_id=reservation_id, connection=unit.connection
            )
            if existing is None:
                self._append_usage_event(unit, reservation.usage)
        return reservation

    def _reserve_existing_in_connection(
        self, workspace_id: str, *, reservation_id: str, connection: Connection
    ) -> WorkspaceReservation:
        workspace = self.get(workspace_id, connection=connection)
        usage, _created = self._acquire_reservation_in_connection(
            workspace, reservation_id=reservation_id, connection=connection
        )
        return WorkspaceReservation(workspace, usage, False)

    def _reserve_borrowed(
        self, facts: ExistingPathFacts, reservation_id: str, owner_id: str
    ) -> WorkspaceReservation:
        with self._store.write_unit() as unit:
            prior = self._store.workspaces.get_active_by_path(
                facts.path, connection=unit.connection
            )
            existing = (
                None
                if prior is None
                else self._store.workspaces.get_active_usage(
                    prior.workspace_id,
                    holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
                    holder_id=reservation_id,
                    connection=unit.connection,
                )
            )
            reservation = self._reserve_borrowed_in_connection(
                facts,
                reservation_id=reservation_id,
                owner_id=owner_id,
                connection=unit.connection,
            )
            created_workspace = reservation.created
            usage = reservation.usage
            self._append_creation_events(
                unit,
                workspace=reservation.workspace if created_workspace else None,
                usage=usage if existing is None else None,
            )
        return reservation

    def _reserve_borrowed_in_connection(
        self,
        facts: ExistingPathFacts,
        *,
        reservation_id: str,
        owner_id: str,
        connection: Connection,
    ) -> WorkspaceReservation:
        workspace = self._store.workspaces.get_active_by_path(facts.path, connection=connection)
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
            self._store.workspaces.create(workspace, connection=connection)
            created_workspace = True
        elif workspace.ownership_kind != WorkspaceOwnershipKind.BORROWED.value:
            raise WorkspaceOwnershipConflict(workspace.workspace_id, "path_already_owned")
        usage, _created_usage = self._acquire_reservation_in_connection(
            workspace, reservation_id=reservation_id, connection=connection
        )
        return WorkspaceReservation(workspace, usage, created_workspace)

    def _create_unique_intent(
        self,
        request: WorkspaceRequest,
        facts: WorktreeCreationFacts,
        *,
        reservation_id: str,
        owner_id: str,
        connection: Connection,
    ) -> WorkspaceReservation:
        workspace_id = self._id_factory()
        record = WorkspaceRecord(
            workspace_id=workspace_id,
            ownership_kind=WorkspaceOwnershipKind.THEATER.value,
            owner_id=owner_id,
            path=worktree_path(facts.canonical_repository_root, workspace_id),
            canonical_repository_root=facts.canonical_repository_root,
            branch=branch_name(workspace_id),
            resolved_base_commit=facts.resolved_base_commit,
            state=WorkspaceState.CREATING.value,
            creation_operation_id=reservation_id,
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        return self._persist_creation_intent(record, reservation_id, connection=connection)

    def _create_named_intent_or_join(
        self,
        request: WorkspaceRequest,
        facts: WorktreeCreationFacts,
        *,
        reservation_id: str,
        owner_id: str,
        connection: Connection,
    ) -> WorkspaceReservation:
        assert isinstance(request.worktree, str)
        name = request.worktree
        # The intent stores a deterministic path/branch before Git runs, so
        # validate the name before deriving either persisted value.
        validate_name(name)
        existing = self._store.workspaces.get_active_named(
            facts.canonical_repository_root, name, connection=connection
        )
        if existing is not None:
            self._require_active(existing)
            if (
                request.base_ref is not None
                and existing.resolved_base_commit != facts.resolved_base_commit
            ):
                raise WorkspaceOwnershipConflict(existing.workspace_id, "named_base_mismatch")
            verify_named_worktree(
                repo_root=facts.canonical_repository_root,
                name=name,
                expected_path=existing.path,
                expected_branch=existing.branch or "",
            )
            return self._reserve_existing_in_connection(
                existing.workspace_id, reservation_id=reservation_id, connection=connection
            )
        workspace_id = self._id_factory()
        record = WorkspaceRecord(
            workspace_id=workspace_id,
            ownership_kind=WorkspaceOwnershipKind.THEATER.value,
            owner_id=owner_id,
            path=named_worktree_path(facts.canonical_repository_root, name),
            canonical_repository_root=facts.canonical_repository_root,
            branch=named_branch_name(name),
            resolved_base_commit=facts.resolved_base_commit,
            name=name,
            state=WorkspaceState.CREATING.value,
            creation_operation_id=reservation_id,
            created_at=self._clock(),
            updated_at=self._clock(),
        )
        return self._persist_creation_intent(record, reservation_id, connection=connection)

    def _persist_creation_intent(
        self, record: WorkspaceRecord, reservation_id: str, *, connection: Connection
    ) -> WorkspaceReservation:
        self._store.workspaces.create(record, connection=connection)
        usage = WorkspaceUsageRecord(
            usage_id=self._id_factory(),
            workspace_id=record.workspace_id,
            holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
            holder_id=reservation_id,
            acquired_at=self._clock(),
        )
        if not self._store.workspaces.acquire_creation_usage(usage, connection=connection):
            raise RuntimeError("workspace creation intent disappeared during its write unit")
        return WorkspaceReservation(record, usage, True)

    def _acquire_reservation_in_connection(
        self, workspace: WorkspaceRecord, *, reservation_id: str, connection: Connection
    ) -> tuple[WorkspaceUsageRecord, bool]:
        self._require_active(workspace)
        existing = self._store.workspaces.get_active_usage(
            workspace.workspace_id,
            holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
            holder_id=reservation_id,
            connection=connection,
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
        if not self._store.workspaces.acquire_usage(usage, connection=connection):
            current = self.get(workspace.workspace_id, connection=connection)
            raise WorkspaceDeleting(current.workspace_id, current.state)
        return usage, True

    @staticmethod
    def _create_from_intent(record: WorkspaceRecord) -> str:
        if record.canonical_repository_root is None or record.resolved_base_commit is None:
            raise GitFactsError("workspace creation intent lacks exact Git facts")
        if record.name is None:
            return create_worktree(
                repo_root=record.canonical_repository_root,
                child_id=record.workspace_id,
                base_branch=record.resolved_base_commit,
            )
        path, branch = create_named_worktree(
            repo_root=record.canonical_repository_root,
            name=record.name,
            base_branch=record.resolved_base_commit,
        )
        if branch != record.branch:
            raise GitFactsError("named workspace branch differs from the durable intent")
        return path

    @staticmethod
    def _verify_created_path(actual: str, expected: str) -> None:
        if actual != expected:
            raise GitFactsError("Git created a workspace at a path other than the durable intent")

    @staticmethod
    def _verify_created_base(actual: str, expected: str | None) -> None:
        if actual != expected:
            raise GitFactsError("created workspace HEAD does not equal its resolved base commit")

    def _reconcile_creation_intent(self, workspace_id: str) -> bool:
        record = self.get(workspace_id)
        if record.state != WorkspaceState.CREATING.value:
            return record.state == WorkspaceState.ACTIVE.value
        valid = False
        try:
            inspection = inspect_registered_worktree(record)
            valid = inspection.head_commit == record.resolved_base_commit
        except GitFactsError:
            valid = False
        timestamp = self._clock()
        with self._store.write_unit() as unit:
            if valid:
                changed = self._store.workspaces.mark_creation_ready(
                    record.workspace_id,
                    operation_id=record.creation_operation_id or "",
                    updated_at=timestamp,
                    connection=unit.connection,
                )
            else:
                changed = self._store.workspaces.mark_creation_reconcile(
                    record.workspace_id,
                    operation_id=record.creation_operation_id or "",
                    updated_at=timestamp,
                    connection=unit.connection,
                )
            if changed:
                updated = self.get(record.workspace_id, connection=unit.connection)
                self._append_workspace_event(unit, updated)
        return valid and changed

    def _reconcile_named_workspace(self, record: WorkspaceRecord) -> bool:
        if record.name is None or record.canonical_repository_root is None or record.branch is None:
            return False
        try:
            verify_named_worktree(
                repo_root=record.canonical_repository_root,
                name=record.name,
                expected_path=record.path,
                expected_branch=record.branch,
            )
            inspection = inspect_registered_worktree(record)
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
                self._append_workspace_event(unit, updated)
        return changed

    @staticmethod
    def _cleanup_recovery_state(record: WorkspaceRecord) -> str:
        """Inspect only: an ambiguous cleanup is never replayed on restart."""
        try:
            inspect_registered_worktree(record)
        except GitFactsError:
            if record.canonical_repository_root is None:
                return WorkspaceState.RECONCILE.value
            try:
                validate_canonical_repository(record.canonical_repository_root)
            except GitFactsError:
                return WorkspaceState.RECONCILE.value
            if not Path(record.path).exists():
                return WorkspaceState.REMOVED.value
            return WorkspaceState.RECONCILE.value
        return WorkspaceState.RECONCILE.value

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
        self._store.journal.append_group(
            unit, [self._workspace_event(unit, record, revision=revision)]
        )

    def _workspace_event(
        self,
        unit: WriteUnit,
        record: WorkspaceRecord,
        *,
        revision: int | None = None,
    ) -> JournalEventRecord:
        value = revision or self._store.journal.current_sequence(connection=unit.connection) + 1
        return JournalEventRecord(
            kind="workspace.updated",
            entity_id=record.workspace_id,
            entity_revision=value,
            payload=self.project(record, connection=unit.connection),
            recorded_at=record.updated_at,
        )

    def _append_usage_event(self, unit: WriteUnit, usage: WorkspaceUsageRecord) -> None:
        revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        self._store.journal.append_group(
            unit,
            [
                workspace_usage_event(
                    self._store,
                    usage,
                    unit.connection,
                    revision=revision,
                    recorded_at=(
                        usage.released_at if usage.released_at is not None else usage.acquired_at
                    ),
                )
            ],
        )

    def _append_handoff_event(
        self,
        unit: WriteUnit,
        participant: WorkspaceUsageRecord,
    ) -> None:
        revision = self._store.journal.current_sequence(connection=unit.connection) + 1
        self._store.journal.append_group(
            unit,
            [
                workspace_usage_event(
                    self._store,
                    participant,
                    unit.connection,
                    revision=revision,
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
                workspace_usage_event(
                    self._store,
                    usage,
                    unit.connection,
                    revision=revision,
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
