"""Workspace creation recovery: crash, restart, cancellation, partial results and reconciliation."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.workspace_services import (
    git,
    make_repository,
    open_workspace_services,
    persist_creation_intent,
    settled,
)
from theater.daemon.operations import OperationService
from theater.daemon.persistence.store import Store
from theater.daemon.runtime.public_recovery import (
    fail_proven_undispatched,
    reconcile_workspace_lifecycle,
)
from theater.daemon.worktrees import cleanup as cleanup_module
from theater.daemon.worktrees import identity
from theater.daemon.worktrees.named import create_named_worktree
from theater.daemon.worktrees.service import (
    WorkspaceRequest,
    WorkspaceService,
)
from theater.daemon.worktrees.unique import create_worktree
from theater.models import (
    LaunchReservationRecord,
    PublicOperationRecord,
    PublicOperationState,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
)


@pytest.fixture
def repository(tmp_path: Path) -> str:
    return make_repository(tmp_path)


@pytest.fixture
def workspace_services(tmp_path: Path):
    with open_workspace_services(tmp_path) as services:
        yield services


async def test_reconcile_promotes_a_migrated_named_workspace_from_exact_git_facts(
    repository: str, workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _operations, service = workspace_services
    path, branch = create_named_worktree(repo_root=repository, name="retained", base_branch="HEAD")
    with store.write_unit() as unit:
        store.workspaces.create(
            WorkspaceRecord(
                workspace_id="rc9-named-retained",
                ownership_kind=WorkspaceOwnershipKind.THEATER.value,
                owner_id="theater",
                path=path,
                canonical_repository_root=repository,
                branch=branch,
                name="retained",
                state=WorkspaceState.RECONCILE.value,
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )

    original = WorkspaceService._verify_named_workspace
    main_thread = threading.get_ident()

    def guarded(record):
        assert not store._db._write_unit_active
        assert threading.get_ident() != main_thread
        return original(record)

    monkeypatch.setattr(WorkspaceService, "_verify_named_workspace", staticmethod(guarded))
    assert await service.reconcile_retained_workspaces() == ("rc9-named-retained",)
    reconciled = service.get("rc9-named-retained")
    assert reconciled.state == WorkspaceState.ACTIVE.value
    assert reconciled.resolved_base_commit == git(path, "rev-parse", "HEAD")


async def test_recovery_promotes_an_exact_created_intent_without_replaying_git(
    repository: str, workspace_services
) -> None:
    store, _operations, service = workspace_services
    preparation = await service.prepare_for_spawn(WorkspaceRequest(cwd=repository, worktree=True))
    with store.write_unit() as unit:
        reservation = service.reserve_for_spawn(
            preparation,
            reservation_id="creation-intent",
            owner_id="local_operator",
            connection=unit.connection,
        )

    workspace = reservation.workspace
    assert workspace.state == WorkspaceState.CREATING.value
    assert workspace.canonical_repository_root is not None
    assert workspace.resolved_base_commit is not None
    assert (
        create_worktree(
            repo_root=workspace.canonical_repository_root,
            child_id=workspace.workspace_id,
            base_branch=workspace.resolved_base_commit,
        )
        == workspace.path
    )

    assert await service.reconcile_retained_workspaces() == (workspace.workspace_id,)
    reconciled = service.get(workspace.workspace_id)
    assert reconciled.state == WorkspaceState.ACTIVE.value
    assert reconciled.resolved_base_commit == git(workspace.path, "rev-parse", "HEAD")


async def test_cancelled_creation_never_retires_an_inflight_git_worker(
    repository: str, workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _operations, service = workspace_services
    reservation = await persist_creation_intent(
        store,
        service,
        WorkspaceRequest(cwd=repository, worktree=True),
        reservation_id="cancelled-creation",
    )
    workspace = reservation.workspace
    original = WorkspaceService._create_from_intent
    started = threading.Event()
    release = threading.Event()
    main_thread = threading.get_ident()

    def blocked(record: WorkspaceRecord) -> str:
        assert not store._db._write_unit_active
        assert threading.get_ident() != main_thread
        started.set()
        assert release.wait(timeout=5)
        return original(record)

    monkeypatch.setattr(WorkspaceService, "_create_from_intent", staticmethod(blocked))
    task = asyncio.create_task(
        service.materialize_creation(reservation, reservation_id="cancelled-creation")
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        retained = service.get(workspace.workspace_id)
        assert retained.state == WorkspaceState.RECONCILE.value
        assert store.workspaces.active_usages(workspace.workspace_id) == [reservation.usage]
        assert not Path(workspace.path).exists()

        release.set()
        for _ in range(200):
            if Path(workspace.path).is_dir():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the cancelled Git worker did not finish its worktree creation")

        assert await service.reconcile_retained_workspaces() == (workspace.workspace_id,)
        assert service.get(workspace.workspace_id).state == WorkspaceState.ACTIVE.value
    finally:
        release.set()


async def test_branch_only_creation_partial_cleanup_is_fenced_and_retryable(
    repository: str, workspace_services
) -> None:
    store, operations, service = workspace_services
    request = WorkspaceRequest(cwd=repository, worktree="branch-only-partial")
    reservation = await persist_creation_intent(
        store,
        service,
        request,
        reservation_id="branch-only-intent",
    )
    workspace = reservation.workspace
    assert workspace.branch is not None
    assert workspace.resolved_base_commit is not None
    git(repository, "branch", workspace.branch, workspace.resolved_base_commit)

    assert await service.reconcile_retained_workspaces() == (workspace.workspace_id,)
    assert service.get(workspace.workspace_id).state == WorkspaceState.RECONCILE.value
    service.release_usage(reservation.usage.usage_id, reason="creation_interrupted")

    git(repository, "commit", "--allow-empty", "-m", "move partial branch")
    git(repository, "branch", "-f", workspace.branch, "HEAD")

    refused = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-branch-only-moved",
        params={
            "workspace_id": workspace.workspace_id,
            "force": True,
            "delete_branch": True,
            "force_branch": True,
        },
    )
    refused_record = await settled(operations, refused)
    assert refused_record.state == "failed"
    assert service.get(workspace.workspace_id).state == WorkspaceState.RECONCILE.value
    assert git(repository, "rev-parse", workspace.branch) != workspace.resolved_base_commit

    git(repository, "branch", "-f", workspace.branch, workspace.resolved_base_commit)
    cleaned = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-branch-only-exact",
        params={
            "workspace_id": workspace.workspace_id,
            "force": True,
            "delete_branch": True,
            "force_branch": True,
        },
    )
    cleaned_record = await settled(operations, cleaned)
    assert cleaned_record.state == "succeeded"
    assert service.get(workspace.workspace_id).state == WorkspaceState.REMOVED.value
    assert git(repository, "branch", "--list", workspace.branch) == ""

    retry = await service.reserve(request, reservation_id="branch-only-retry")
    assert retry.workspace.state == WorkspaceState.ACTIVE.value


async def test_metadata_only_creation_partial_is_not_retired_as_absent_and_can_retry(
    repository: str, workspace_services
) -> None:
    store, operations, service = workspace_services
    request = WorkspaceRequest(cwd=repository, worktree="metadata-only-partial")
    reservation = await persist_creation_intent(
        store,
        service,
        request,
        reservation_id="metadata-only-intent",
    )
    workspace = reservation.workspace
    assert workspace.branch is not None
    assert workspace.resolved_base_commit is not None
    path, branch = create_named_worktree(
        repo_root=repository,
        name="metadata-only-partial",
        base_branch=workspace.resolved_base_commit,
    )
    assert (path, branch) == (workspace.path, workspace.branch)
    git(path, "checkout", "--detach")
    git(repository, "branch", "-D", workspace.branch)
    shutil.rmtree(path)

    assert await service.reconcile_retained_workspaces() == (workspace.workspace_id,)
    assert service.get(workspace.workspace_id).state == WorkspaceState.RECONCILE.value
    service.release_usage(reservation.usage.usage_id, reason="creation_interrupted")
    accepted = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-metadata-only",
        params={"workspace_id": workspace.workspace_id, "force": True},
    )
    completed = await settled(operations, accepted)
    assert completed.state == "succeeded"
    assert service.get(workspace.workspace_id).state == WorkspaceState.REMOVED.value

    retry = await service.reserve(request, reservation_id="metadata-only-retry")
    assert retry.workspace.state == WorkspaceState.ACTIVE.value


async def test_crash_before_git_retires_exactly_absent_named_intent_and_allows_reuse(
    repository: str, workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _operations, service = workspace_services
    request = WorkspaceRequest(cwd=repository, worktree="crash-before-git")
    preparation = await service.prepare_for_spawn(request)
    with store.write_unit() as unit:
        reservation = service.reserve_for_spawn(
            preparation,
            reservation_id="crash-before-git",
            owner_id="local_operator",
            connection=unit.connection,
        )

    workspace = reservation.workspace
    assert workspace.branch is not None
    assert not Path(workspace.path).exists()
    assert git(repository, "branch", "--list", workspace.branch) == ""

    original = identity.inspect_creation_intent
    main_thread = threading.get_ident()

    def guarded(record):
        assert not store._db._write_unit_active
        assert threading.get_ident() != main_thread
        return original(record)

    monkeypatch.setattr(identity, "inspect_creation_intent", guarded)
    assert await service.reconcile_retained_workspaces() == (workspace.workspace_id,)

    retired = service.get(workspace.workspace_id)
    assert retired.state == WorkspaceState.REMOVED.value
    assert store.workspaces.active_usages(workspace.workspace_id) == []

    retry = await service.prepare_for_spawn(request)
    with store.write_unit() as unit:
        recreated = service.reserve_for_spawn(
            retry,
            reservation_id="crash-before-git-retry",
            owner_id="local_operator",
            connection=unit.connection,
        )
    assert recreated.workspace.workspace_id != workspace.workspace_id
    assert recreated.workspace.state == WorkspaceState.CREATING.value


async def test_reconciliation_pages_past_unchanged_early_records(
    workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _operations, service = workspace_services
    with store.write_unit() as unit:
        for index in range(501):
            store.workspaces.create(
                WorkspaceRecord(
                    workspace_id=f"stale-{index:03d}",
                    ownership_kind=WorkspaceOwnershipKind.THEATER.value,
                    owner_id="theater",
                    path=f"/missing/stale-{index:03d}",
                    canonical_repository_root="/missing/repository",
                    branch=f"theater/named/stale-{index:03d}",
                    name=f"stale-{index:03d}",
                    state=WorkspaceState.RECONCILE.value,
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )

    visited: list[str] = []

    async def unreconciled(record: WorkspaceRecord) -> bool:
        visited.append(record.workspace_id)
        return False

    monkeypatch.setattr(service, "_reconcile_named_workspace", unreconciled)
    assert await asyncio.wait_for(service.reconcile_retained_workspaces(), timeout=2) == ()
    assert visited == [f"stale-{index:03d}" for index in range(501)]


async def test_recovery_rollback_cleans_a_created_workspace_off_thread(
    repository: str, workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="recovery-rollback"
    )
    service.release_usage(reservation.usage.usage_id, reason="restart_before_dispatch")
    original = WorkspaceService._cleanup_creation_rollback
    main_thread = threading.get_ident()

    def guarded(record):
        assert not store._db._write_unit_active
        assert threading.get_ident() != main_thread
        return original(record)

    monkeypatch.setattr(WorkspaceService, "_cleanup_creation_rollback", staticmethod(guarded))
    assert await service.rollback_created_reservation_after_recovery(
        workspace_id=reservation.workspace.workspace_id,
        reservation_id="recovery-rollback",
    )
    assert service.get(reservation.workspace.workspace_id).state == WorkspaceState.REMOVED.value


async def test_restart_failure_persists_creation_rollback_before_async_cleanup(
    daemon, repository: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation_id = "recovery-workspace-operation"
    reservation = await daemon.workspace_service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True),
        reservation_id=operation_id,
    )
    participant = daemon.registry.create_spawned(
        harness="codex",
        cwd=reservation.workspace.path,
        has_prompt=False,
    )
    daemon.jobs.create(
        handle="recovery-workspace-job",
        caller_id=None,
        target_id=participant.id,
        kind="spawn",
        actor_client_id="operator-a",
    )
    timestamp = 10.0
    operation = PublicOperationRecord(
        operation_id=operation_id,
        kind="spawn",
        actor_client_id="operator-a",
        actor_participant_id=None,
        target_ids=(participant.id,),
        state=PublicOperationState.ACCEPTED.value,
        phase="launch_reserved",
        job_handle="recovery-workspace-job",
        created_at=timestamp,
        updated_at=timestamp,
    )
    with daemon.store.write_unit() as unit:
        daemon.store.operations.create(operation, connection=unit.connection)
        daemon.store.operations.reserve_launch(
            LaunchReservationRecord(
                operation_id=operation_id,
                participant_id=participant.id,
                provider_id="provider-a",
                workspace_usage_id=reservation.usage.usage_id,
                adapter="codex",
                phase="reserved",
                launch_facts={},
                artifact_refs=(),
                dispatch_marker=None,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    resume_rollback = daemon.workspace_service.rollback_created_reservation_after_recovery

    async def crash_before_cleanup(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        daemon.workspace_service,
        "rollback_created_reservation_after_recovery",
        crash_before_cleanup,
    )
    with pytest.raises(asyncio.CancelledError):
        await fail_proven_undispatched(daemon, operation)

    stranded = daemon.workspace_service.get(reservation.workspace.workspace_id)
    assert stranded.state == WorkspaceState.DELETING.value
    assert stranded.deletion_operation_id == operation_id
    assert stranded.deletion_token is not None
    assert stranded.cleanup_force is True
    assert stranded.cleanup_delete_branch is True
    assert stranded.cleanup_force_branch is True
    assert daemon.store.workspaces.active_usages(stranded.workspace_id) == []
    assert daemon.store.operations.get(operation_id).state == PublicOperationState.FAILED.value

    assert await resume_rollback(
        workspace_id=stranded.workspace_id,
        reservation_id=operation_id,
    )
    assert daemon.workspace_service.get(stranded.workspace_id).state == WorkspaceState.REMOVED.value


async def test_restart_recovers_stranded_creation_rollback(repository: str, tmp_path: Path) -> None:
    database_path = tmp_path / "stranded-creation-rollback.db"
    store = Store(database_path)
    operations = OperationService(store, id_factory=lambda: "unused-operation")
    service = WorkspaceService(store, operations, id_factory=lambda: "rollback-token")
    try:
        reservation = await service.reserve(
            WorkspaceRequest(cwd=repository, worktree=True),
            reservation_id="stranded-spawn",
        )
        service.release_usage(reservation.usage.usage_id, reason="daemon_restarted")
        with store.write_unit() as unit:
            store.operations.create(
                PublicOperationRecord(
                    operation_id="stranded-spawn",
                    kind="spawn",
                    actor_client_id="operator-a",
                    actor_participant_id=None,
                    target_ids=("participant-stranded",),
                    state=PublicOperationState.FAILED.value,
                    phase="daemon_restart_before_dispatch",
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )
        prepared = service._begin_creation_rollback(
            workspace_id=reservation.workspace.workspace_id,
            reservation_id="stranded-spawn",
        )
        assert prepared is not None
        deleting, _token = prepared
        result = cleanup_module.cleanup_exact_worktree(
            deleting,
            force=True,
            delete_branch=True,
            force_branch=True,
        )
        assert result.ok
        store.close()

        reopened = Store(database_path)
        recovered_operations = OperationService(reopened)
        recovered_service = WorkspaceService(reopened, recovered_operations)
        daemon = SimpleNamespace(store=reopened, workspace_service=recovered_service)
        assert await reconcile_workspace_lifecycle(daemon) == (reservation.workspace.workspace_id,)
        workspace = recovered_service.get(reservation.workspace.workspace_id)
        assert workspace.state == WorkspaceState.REMOVED.value
        assert workspace.cleanup_result == {
            "worktree_removed": True,
            "branch_removed": True,
            "branch_retained": False,
            "errors": [],
            "uncertain": False,
        }
        reopened.close()
    finally:
        store.close()


async def test_out_of_band_disappearance_requires_reconciliation(
    repository: str, workspace_services
) -> None:
    _store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="reservation-missing"
    )
    workspace = reservation.workspace
    service.release_usage(reservation.usage.usage_id, reason="participant_exit")
    shutil.rmtree(workspace.path)

    accepted = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-missing",
        params={"workspace_id": workspace.workspace_id, "force": True},
    )
    failed = await settled(operations, accepted)
    assert failed.state == "failed"
    assert service.get(workspace.workspace_id).state == "reconcile"
    assert git(repository, "show-ref", "--verify", f"refs/heads/{workspace.branch}")


async def test_indeterminate_git_identity_never_deletes_the_worktree(
    repository: str, workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="reservation-ambiguous"
    )
    workspace = reservation.workspace
    service.release_usage(reservation.usage.usage_id, reason="launch_abandoned")
    original_git = identity._git

    def ambiguous_worktree_list(argv, **kwargs):
        if argv == ["git", "worktree", "list", "--porcelain", "-z"]:
            return subprocess.CompletedProcess(argv, 124, "", "query timed out")
        return original_git(argv, **kwargs)

    monkeypatch.setattr(identity, "_git", ambiguous_worktree_list)
    accepted = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-ambiguous",
        params={"workspace_id": workspace.workspace_id, "force": True},
    )
    failed = await settled(operations, accepted)

    assert failed.state == "failed"
    assert service.get(workspace.workspace_id).state == "reconcile"
    assert Path(workspace.path).is_dir()


async def test_ambiguous_git_mutation_keeps_an_unsettled_operation(
    repository: str, workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="reservation-timeout"
    )
    workspace = reservation.workspace
    service.release_usage(reservation.usage.usage_id, reason="launch_abandoned")
    original_git = cleanup_module._git

    def timeout_remove(argv, **kwargs):
        if argv[:3] == ["git", "worktree", "remove"]:
            return subprocess.CompletedProcess(argv, 124, "", "git timed out")
        return original_git(argv, **kwargs)

    monkeypatch.setattr(cleanup_module, "_git", timeout_remove)
    accepted = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-timeout",
        params={"workspace_id": workspace.workspace_id, "force": True},
    )
    assert operations.owned_tasks
    await operations.owned_tasks[0]
    operation = operations.get(str(accepted["operation_id"]))

    assert operation.state == "uncertain"
    assert service.get(workspace.workspace_id).state == "reconcile"
    assert Path(workspace.path).is_dir()
