"""Workspace request preparation and durable reservation."""

from __future__ import annotations

from sqlalchemy import Connection

from theater.daemon import workers
from theater.daemon.worktrees import identity
from theater.daemon.worktrees.identity import (
    ExistingPathFacts,
    GitFactsError,
    WorktreeCreationFacts,
    WorktreeInspection,
    inspect_existing_path,
    inspect_registered_worktree,
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
from theater.daemon.worktrees.service_parts._common import (
    WorkspaceDeleting,
    WorkspaceOwnershipConflict,
    WorkspacePreparation,
    WorkspaceRequest,
    WorkspaceReservation,
)
from theater.daemon.worktrees.service_parts._host import WorkspaceHost
from theater.daemon.worktrees.unique import create_worktree
from theater.models import (
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
)


class WorkspaceReservationFlow(WorkspaceHost):
    async def reserve(
        self,
        request: WorkspaceRequest,
        *,
        reservation_id: str,
        owner_id: str = "local_operator",
    ) -> WorkspaceReservation:
        preparation = await self.prepare_for_spawn(request)
        if request.workspace_id is not None:
            return self._reserve_existing(request.workspace_id, reservation_id)
        with self._store.write_unit() as unit:
            prior_workspace = self._prior_workspace_for_preparation(
                preparation, connection=unit.connection
            )
            prior_usage = (
                None
                if prior_workspace is None
                else self._store.workspaces.get_active_usage(
                    prior_workspace.workspace_id,
                    holder_kind=WorkspaceUsageHolderKind.RESERVATION.value,
                    holder_id=reservation_id,
                    connection=unit.connection,
                )
            )
            reservation = self.reserve_for_spawn(
                preparation,
                reservation_id=reservation_id,
                owner_id=owner_id,
                connection=unit.connection,
            )
            if preparation.existing_path_facts is not None:
                self._journal.append_creation(
                    unit,
                    workspace=reservation.workspace if reservation.created else None,
                    usage=reservation.usage if prior_usage is None else None,
                )
        if reservation.workspace.state == WorkspaceState.CREATING.value:
            return await self.materialize_creation(reservation, reservation_id=reservation_id)
        return reservation

    async def prepare_for_spawn(self, request: WorkspaceRequest) -> WorkspacePreparation:
        """Resolve all filesystem/Git facts before operation admission begins."""
        self._validate_request(request)
        if request.workspace_id is not None:
            return WorkspacePreparation(request)
        assert request.cwd is not None
        if request.worktree is True:
            facts = await workers.to_thread(
                identity.resolve_creation_facts,
                request.cwd,
                request.base_ref,
                label="workspace.unique.resolve",
            )
            return WorkspacePreparation(request, creation_facts=facts)
        if isinstance(request.worktree, str):
            facts = await workers.to_thread(
                self._resolve_named_creation_facts,
                request.cwd,
                request.base_ref,
                request.worktree,
                label="workspace.named.resolve",
            )
            existing = self._store.workspaces.get_active_named(
                facts.canonical_repository_root, request.worktree
            )
            if existing is not None:
                await workers.to_thread(
                    self._verify_named_workspace,
                    existing,
                    label="workspace.named.verify",
                )
            return WorkspacePreparation(
                request,
                creation_facts=facts,
                named_workspace=existing,
            )
        facts = await workers.to_thread(
            inspect_existing_path,
            request.cwd,
            label="workspace.reserve.inspect",
        )
        return WorkspacePreparation(request, existing_path_facts=facts)

    def reserve_for_spawn(
        self,
        preparation: WorkspacePreparation,
        *,
        reservation_id: str,
        owner_id: str,
        connection: Connection,
    ) -> WorkspaceReservation:
        """Persist previously prepared workspace facts in a short write unit."""
        request = preparation.request
        self._validate_request(request)
        if request.workspace_id is not None:
            return self._reserve_existing_in_connection(
                request.workspace_id, reservation_id=reservation_id, connection=connection
            )
        assert request.cwd is not None
        if request.worktree is True or isinstance(request.worktree, str):
            creation_facts = preparation.creation_facts
            if creation_facts is None:
                raise ValueError("workspace creation requires pre-resolved Git facts")
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
                prepared_named=preparation.named_workspace,
                reservation_id=reservation_id,
                owner_id=owner_id,
                connection=connection,
            )
        existing_facts = preparation.existing_path_facts
        if existing_facts is None:
            raise ValueError("borrowed workspace requires pre-inspected path facts")
        return self._reserve_borrowed_in_connection(
            existing_facts, reservation_id=reservation_id, owner_id=owner_id, connection=connection
        )

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
                self._journal.append_usage(unit, reservation.usage)
        return reservation

    def _reserve_existing_in_connection(
        self, workspace_id: str, *, reservation_id: str, connection: Connection
    ) -> WorkspaceReservation:
        workspace = self.get(workspace_id, connection=connection)
        usage, _created = self._acquire_reservation_in_connection(
            workspace, reservation_id=reservation_id, connection=connection
        )
        return WorkspaceReservation(workspace, usage, False)

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
        prepared_named: WorkspaceRecord | None,
        reservation_id: str,
        owner_id: str,
        connection: Connection,
    ) -> WorkspaceReservation:
        assert isinstance(request.worktree, str)
        name = request.worktree
        existing = self._store.workspaces.get_active_named(
            facts.canonical_repository_root, name, connection=connection
        )
        if existing is not None:
            if prepared_named is None or not self._same_named_workspace(existing, prepared_named):
                raise WorkspaceOwnershipConflict(existing.workspace_id, "named_workspace_changed")
            self._require_active(existing)
            if (
                request.base_ref is not None
                and existing.resolved_base_commit != facts.resolved_base_commit
            ):
                raise WorkspaceOwnershipConflict(existing.workspace_id, "named_base_mismatch")
            return self._reserve_existing_in_connection(
                existing.workspace_id, reservation_id=reservation_id, connection=connection
            )
        if prepared_named is not None:
            raise WorkspaceOwnershipConflict(prepared_named.workspace_id, "named_workspace_changed")
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

    @staticmethod
    def _resolve_named_creation_facts(
        cwd: str, base_ref: str | None, name: str
    ) -> WorktreeCreationFacts:
        validate_name(name)
        return identity.resolve_creation_facts(cwd, base_ref)

    @staticmethod
    def _verify_named_workspace(record: WorkspaceRecord) -> WorktreeInspection:
        if record.name is None or record.canonical_repository_root is None or record.branch is None:
            raise GitFactsError("named workspace lacks deterministic identity facts")
        verify_named_worktree(
            repo_root=record.canonical_repository_root,
            name=record.name,
            expected_path=record.path,
            expected_branch=record.branch,
        )
        return inspect_registered_worktree(record)

    @staticmethod
    def _same_named_workspace(current: WorkspaceRecord, prepared: WorkspaceRecord) -> bool:
        return (
            current.workspace_id == prepared.workspace_id
            and current.state == WorkspaceState.ACTIVE.value
            and current.path == prepared.path
            and current.canonical_repository_root == prepared.canonical_repository_root
            and current.branch == prepared.branch
            and current.resolved_base_commit == prepared.resolved_base_commit
            and current.name == prepared.name
        )

    def _prior_workspace_for_preparation(
        self, preparation: WorkspacePreparation, *, connection: Connection
    ) -> WorkspaceRecord | None:
        request = preparation.request
        if request.workspace_id is not None:
            return self._store.workspaces.get(request.workspace_id, connection=connection)
        if preparation.named_workspace is not None:
            return self._store.workspaces.get(
                preparation.named_workspace.workspace_id, connection=connection
            )
        if preparation.existing_path_facts is not None:
            return self._store.workspaces.get_active_by_path(
                preparation.existing_path_facts.path, connection=connection
            )
        return None

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
