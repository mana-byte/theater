"""Focused durable workspace lifecycle and exact Git cleanup checks."""

from __future__ import annotations

import asyncio
import itertools
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.test_rc10_provider_controls import _online, _target
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.workspace_handlers import WORKSPACE_HANDLERS, workspaces_register
from theater.daemon.operations import OperationService
from theater.daemon.operations.reconciliation import DurableEvidenceReconciler
from theater.daemon.persistence.store import Store
from theater.daemon.rpc.participants import terminate_participant
from theater.daemon.runtime.public_recovery import (
    fail_proven_undispatched,
    reconcile_workspace_lifecycle,
)
from theater.daemon.runtime.recovery import reconcile_public_control_operations
from theater.daemon.schema import orchestration_events
from theater.daemon.worktrees import cleanup as cleanup_module
from theater.daemon.worktrees import identity
from theater.daemon.worktrees.named import create_named_worktree
from theater.daemon.worktrees.service import (
    WorkspaceDeleting,
    WorkspaceInUse,
    WorkspaceOwnershipConflict,
    WorkspaceRequest,
    WorkspaceService,
)
from theater.daemon.worktrees.unique import create_worktree
from theater.frontend.capabilities import METHOD_CATALOG, ConnectionChannel, ConnectionRole
from theater.frontend.dto.workspaces import Workspace as PublicWorkspace
from theater.frontend.dto.workspaces import WorkspaceState as PublicWorkspaceState
from theater.frontend.schemas import validator_for
from theater.models import (
    BadRequest,
    LaunchReservationRecord,
    PublicOperationRecord,
    PublicOperationState,
    WorkspaceOwnershipKind,
    WorkspaceRecord,
    WorkspaceState,
)


def _git(repo: str | Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> str:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("initial\n")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    return str(root)


@pytest.fixture
def workspace_services(tmp_path: Path):
    store = Store(tmp_path / "workspaces.db")
    operation_ids = (f"operation-{index}" for index in itertools.count(1))
    value_ids = (f"workspace-value-{index}" for index in itertools.count(1))
    operations = OperationService(store, id_factory=lambda: next(operation_ids))
    service = WorkspaceService(store, operations, id_factory=lambda: next(value_ids))
    try:
        yield store, operations, service
    finally:
        store.close()


def _external_params(path: Path) -> dict[str, object]:
    return {
        "ownership_kind": "frontend",
        "owner_id": "frontend-a",
        "path": str(path),
    }


async def _settled(operations: OperationService, accepted: object):
    assert isinstance(accepted, dict)
    record, timed_out = await operations.wait(str(accepted["operation_id"]), wait_seconds=5)
    assert not timed_out
    return record


async def _persist_creation_intent(
    store: Store,
    service: WorkspaceService,
    request: WorkspaceRequest,
    *,
    reservation_id: str,
):
    preparation = await service.prepare_for_spawn(request)
    with store.write_unit() as unit:
        return service.reserve_for_spawn(
            preparation,
            reservation_id=reservation_id,
            owner_id="local_operator",
            connection=unit.connection,
        )


async def test_external_deletion_fence_serializes_usage_and_exact_token(
    tmp_path: Path, workspace_services
) -> None:
    store, _operations, service = workspace_services
    directory = tmp_path / "frontend-workspace"
    directory.mkdir()
    params = _external_params(directory)
    registered = await service.register(
        client_id="operator-a", idempotency_key="register-a", params=params
    )
    workspace_id = str(registered["workspace_id"])
    reservation = await service.reserve(
        WorkspaceRequest(workspace_id=workspace_id), reservation_id="reservation-a"
    )

    with pytest.raises(WorkspaceInUse):
        service.prepare_external_delete(
            client_id="operator-a",
            idempotency_key="prepare-blocked",
            params={"workspace_id": workspace_id},
        )
    participant_usage = service.handoff_usage(
        reservation.usage.usage_id, participant_id="participant-a"
    )
    assert store.workspaces.active_usages(workspace_id) == [participant_usage]
    service.release_usage(participant_usage.usage_id, reason="participant_exit")

    prepared = service.prepare_external_delete(
        client_id="operator-a",
        idempotency_key="prepare-a",
        params={"workspace_id": workspace_id},
    )
    assert prepared["revision"] > 0
    token = str(prepared["token"])
    with pytest.raises(WorkspaceDeleting):
        await service.reserve(
            WorkspaceRequest(workspace_id=workspace_id), reservation_id="reservation-b"
        )
    with pytest.raises(WorkspaceOwnershipConflict):
        service.cancel_external_delete(
            client_id="operator-a",
            idempotency_key="cancel-wrong",
            params={"workspace_id": workspace_id, "token": "wrong-token"},
        )

    canceled = service.cancel_external_delete(
        client_id="operator-a",
        idempotency_key="cancel-a",
        params={"workspace_id": workspace_id, "token": token},
    )
    assert canceled["workspace"]["state"] == "active"
    prepared = service.prepare_external_delete(
        client_id="operator-a",
        idempotency_key="prepare-b",
        params={"workspace_id": workspace_id},
    )
    confirmed = service.confirm_external_delete(
        client_id="operator-a",
        idempotency_key="confirm-a",
        params={"workspace_id": workspace_id, "token": prepared["token"]},
    )
    assert confirmed["workspace"]["state"] == "removed"
    assert directory.is_dir(), "external workspace deletion is never performed by Theater"


async def test_participant_usage_survives_restart_and_still_blocks_deletion(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart.db"
    directory = tmp_path / "retained-workspace"
    directory.mkdir()
    ids = (f"restart-value-{index}" for index in itertools.count(1))

    first_store = Store(database_path)
    first_operations = OperationService(first_store, id_factory=lambda: next(ids))
    first_service = WorkspaceService(first_store, first_operations, id_factory=lambda: next(ids))
    registered = await first_service.register(
        client_id="operator-a",
        idempotency_key="register-restart",
        params=_external_params(directory),
    )
    reservation = await first_service.reserve(
        WorkspaceRequest(workspace_id=str(registered["workspace_id"])),
        reservation_id="reservation-restart",
    )
    participant_usage = first_service.handoff_usage(
        reservation.usage.usage_id, participant_id="participant-restart"
    )
    first_store.close()

    second_store = Store(database_path)
    second_operations = OperationService(second_store, id_factory=lambda: next(ids))
    second_service = WorkspaceService(second_store, second_operations, id_factory=lambda: next(ids))
    try:
        assert second_store.workspaces.active_usages(str(registered["workspace_id"])) == [
            participant_usage
        ]
        with pytest.raises(WorkspaceInUse):
            second_service.prepare_external_delete(
                client_id="operator-a",
                idempotency_key="prepare-after-restart",
                params={"workspace_id": registered["workspace_id"]},
            )
    finally:
        second_store.close()


async def test_registration_replay_does_not_reinspect_a_changed_path(
    tmp_path: Path, workspace_services
) -> None:
    _store, _operations, service = workspace_services
    directory = tmp_path / "registered-once"
    directory.mkdir()
    params = _external_params(directory)
    first = await service.register(
        client_id="operator-a",
        idempotency_key="register-replay",
        params=params,
    )
    directory.rmdir()

    replay = await service.register(
        client_id="operator-a",
        idempotency_key="register-replay",
        params=params,
    )

    assert replay == first


async def test_linked_checkout_head_is_captured_for_unique_workspace(
    repository: str, workspace_services
) -> None:
    _store, _operations, service = workspace_services
    linked = Path(repository).parent / "initiating-linked"
    _git(repository, "worktree", "add", "-b", "initiating", str(linked), "HEAD")
    (linked / "linked.txt").write_text("linked head\n")
    _git(linked, "add", "linked.txt")
    _git(linked, "commit", "-m", "linked head")
    linked_head = _git(linked, "rev-parse", "HEAD")
    assert linked_head != _git(repository, "rev-parse", "HEAD")

    reservation = await service.reserve(
        WorkspaceRequest(cwd=str(linked), worktree=True),
        reservation_id="reservation-linked",
    )

    workspace = reservation.workspace
    assert workspace.canonical_repository_root == repository
    assert workspace.resolved_base_commit == linked_head
    assert _git(workspace.path, "rev-parse", "HEAD") == linked_head


async def test_dirty_cleanup_refusal_then_explicit_force(
    repository: str, workspace_services
) -> None:
    _store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="reservation-dirty"
    )
    service.release_usage(reservation.usage.usage_id, reason="launch_abandoned")
    dirty = Path(reservation.workspace.path) / "untracked.txt"
    dirty.write_text("do not discard implicitly\n")

    refused = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-dirty-refused",
        params={"workspace_id": reservation.workspace.workspace_id},
    )
    failed = await _settled(operations, refused)
    assert failed.state == "failed"
    assert service.get(reservation.workspace.workspace_id).state == "reconcile"
    assert dirty.exists()

    forced = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-dirty-force",
        params={"workspace_id": reservation.workspace.workspace_id, "force": True},
    )
    succeeded = await _settled(operations, forced)
    assert succeeded.state == "succeeded"
    assert succeeded.result["branch_retained"] is True
    assert not Path(reservation.workspace.path).exists()


@pytest.mark.parametrize("contents", ["clean", "dirty", "unmerged", "shared", "in_use"])
async def test_kill_cleans_only_unused_unique_workspaces(daemon, repository, monkeypatch, contents):
    participant_id = _target(daemon)
    _online(monkeypatch, daemon)
    reservation = await daemon.workspace_service.reserve(
        WorkspaceRequest(cwd=repository, worktree="shared" if contents == "shared" else True),
        reservation_id="kill-workspace",
    )
    workspace = reservation.workspace
    daemon.workspace_service.handoff_usage(
        reservation.usage.usage_id, participant_id=participant_id
    )
    other_usage = None
    if contents == "in_use":
        other = await daemon.workspace_service.reserve(
            WorkspaceRequest(workspace_id=workspace.workspace_id), reservation_id="other-launch"
        )
        other_usage = other.usage.usage_id
    participant = daemon.registry.get(participant_id)
    participant.workspace_id = workspace.workspace_id
    participant.cwd = workspace.path
    daemon.store.upsert_participant(participant)
    work = Path(workspace.path) / "work.txt"
    if contents in {"dirty", "unmerged"}:
        work.write_text("retain this work\n")
    if contents == "unmerged":
        _git(workspace.path, "add", "work.txt")
        _git(workspace.path, "commit", "-m", "child work")

    async def terminate(_provider, generation, method, params):
        assert method == "terminal.terminate"
        assert Path(workspace.path).exists()
        assert daemon.store.workspaces.active_usages(workspace.workspace_id)
        return {
            "operation_id": params["operation_id"],
            "provider_generation": generation,
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "accepted",
            "exit_confirmed": True,
        }

    monkeypatch.setattr(daemon.terminal_service.connections, "request", terminate)
    result = await terminate_participant(daemon, participant_id, caller_id="cli")
    assert result["killed"] is True
    usages = daemon.store.workspaces.active_usages(workspace.workspace_id)
    assert [usage.usage_id for usage in usages] == ([other_usage] if other_usage else [])
    retained = contents in {"dirty", "shared", "in_use"}
    assert Path(workspace.path).exists() is retained
    assert (workspace.path in _git(repository, "worktree", "list", "--porcelain")) is retained
    assert bool(_git(repository, "branch", "--list", workspace.branch)) is (contents != "clean")
    if contents == "shared":
        assert "workspace_cleanup" not in result
    else:
        assert result["workspace_cleanup"]["state"] == (
            "succeeded" if contents == "clean" else "retained" if contents == "in_use" else "failed"
        )
    if contents == "dirty":
        assert work.read_text() == "retain this work\n"
    if contents == "unmerged":
        assert _git(repository, "show", f"{workspace.branch}:work.txt") == "retain this work"


async def test_missing_retained_branch_is_a_terminal_cleanup_failure(
    repository: str, workspace_services
) -> None:
    _store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True),
        reservation_id="reservation-missing-retained-branch",
    )
    workspace = reservation.workspace
    service.release_usage(reservation.usage.usage_id, reason="participant_exit")
    first = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-retain-before-race",
        params={"workspace_id": workspace.workspace_id},
    )
    assert (await _settled(operations, first)).state == PublicOperationState.SUCCEEDED.value
    _git(repository, "branch", "-D", "--", workspace.branch)

    raced = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-retain-after-race",
        params={"workspace_id": workspace.workspace_id},
    )
    await asyncio.gather(*operations.owned_tasks)
    outcome = operations.get(str(raced["operation_id"]))
    assert outcome.state == PublicOperationState.FAILED.value
    assert outcome.error is not None
    assert outcome.error["details"]["branch_retained"] is False
    assert outcome.error["details"]["errors"] == ["the branch requested for retention is missing"]

    reconciled = cleanup_module.inspect_cleanup_result(
        service.get(workspace.workspace_id), delete_branch=False
    )
    assert reconciled.uncertain is False
    assert reconciled.errors == ("the branch requested for retention is missing",)


async def test_cleanup_acceptance_groups_workspace_and_operation_events(
    repository: str, workspace_services
) -> None:
    store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="reservation-events"
    )
    service.release_usage(reservation.usage.usage_id, reason="launch_abandoned")

    accepted = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-events",
        params={"workspace_id": reservation.workspace.workspace_id},
    )

    rows = store.conn.execute(
        select(
            orchestration_events.c.kind,
            orchestration_events.c.entity_id,
            orchestration_events.c.transaction_id,
            orchestration_events.c.event_index,
            orchestration_events.c.ending_sequence,
        )
        .where(
            orchestration_events.c.entity_id.in_(
                [reservation.workspace.workspace_id, accepted["operation_id"]]
            )
        )
        .order_by(orchestration_events.c.sequence.desc())
        .limit(2)
    ).all()

    assert {(row.kind, row.entity_id) for row in rows} == {
        ("workspace.updated", reservation.workspace.workspace_id),
        ("operation.updated", accepted["operation_id"]),
    }
    assert len({row.transaction_id for row in rows}) == 1
    assert {row.event_index for row in rows} == {0, 1}
    assert len({row.ending_sequence for row in rows}) == 1
    await _settled(operations, accepted)


async def test_unmerged_branch_requires_separate_force_branch(
    repository: str, workspace_services
) -> None:
    _store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="reservation-branch"
    )
    workspace = reservation.workspace
    service.release_usage(reservation.usage.usage_id, reason="participant_exit")
    (Path(workspace.path) / "result.txt").write_text("result\n")
    _git(workspace.path, "add", "result.txt")
    _git(workspace.path, "commit", "-m", "unmerged result")

    retained = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-retain-unmerged",
        params={"workspace_id": workspace.workspace_id, "delete_branch": True},
    )
    partial = await _settled(operations, retained)
    assert partial.state == "failed"
    assert partial.error is not None
    assert partial.error["details"]["worktree_removed"] is True
    assert partial.error["details"]["branch_retained"] is True
    assert _git(repository, "show-ref", "--verify", f"refs/heads/{workspace.branch}")

    forced = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-force-branch",
        params={
            "workspace_id": workspace.workspace_id,
            "delete_branch": True,
            "force_branch": True,
        },
    )
    completed = await _settled(operations, forced)
    assert completed.result["branch_removed"] is True


async def test_named_workspace_joins_then_retained_branch_blocks_recreation(
    repository: str, workspace_services
) -> None:
    _store, operations, service = workspace_services
    first = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree="shared"),
        reservation_id="reservation-named-a",
    )
    service.release_usage(first.usage.usage_id, reason="launch_abandoned")

    joined = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree="shared"),
        reservation_id="reservation-named-b",
    )
    assert joined.workspace.workspace_id == first.workspace.workspace_id
    assert joined.workspace.path == first.workspace.path
    assert joined.created is False
    service.release_usage(joined.usage.usage_id, reason="participant_exit")

    accepted = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-named-retain",
        params={"workspace_id": first.workspace.workspace_id},
    )
    completed = await _settled(operations, accepted)
    assert completed.result["worktree_removed"] is True
    assert completed.result["branch_retained"] is True

    with pytest.raises(BadRequest, match="already exists"):
        await service.reserve(
            WorkspaceRequest(cwd=repository, worktree="shared"),
            reservation_id="reservation-named-c",
        )


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
    assert reconciled.resolved_base_commit == _git(path, "rev-parse", "HEAD")


async def test_named_creation_rejects_an_invalid_name_before_persisting_an_intent(
    repository: str, workspace_services
) -> None:
    store, _operations, service = workspace_services

    with pytest.raises(BadRequest, match="must not contain '/'"):
        await service.reserve(
            WorkspaceRequest(cwd=repository, worktree="outside/intent"),
            reservation_id="invalid-named-intent",
        )

    records, cursor = store.workspaces.list_page(cursor=None, limit=10)
    assert records == ()
    assert cursor is None


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
    assert reconciled.resolved_base_commit == _git(workspace.path, "rev-parse", "HEAD")


async def test_cancelled_creation_never_retires_an_inflight_git_worker(
    repository: str, workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _operations, service = workspace_services
    reservation = await _persist_creation_intent(
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
    reservation = await _persist_creation_intent(
        store,
        service,
        request,
        reservation_id="branch-only-intent",
    )
    workspace = reservation.workspace
    assert workspace.branch is not None
    assert workspace.resolved_base_commit is not None
    _git(repository, "branch", workspace.branch, workspace.resolved_base_commit)

    assert await service.reconcile_retained_workspaces() == (workspace.workspace_id,)
    assert service.get(workspace.workspace_id).state == WorkspaceState.RECONCILE.value
    service.release_usage(reservation.usage.usage_id, reason="creation_interrupted")

    _git(repository, "commit", "--allow-empty", "-m", "move partial branch")
    _git(repository, "branch", "-f", workspace.branch, "HEAD")

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
    refused_record = await _settled(operations, refused)
    assert refused_record.state == "failed"
    assert service.get(workspace.workspace_id).state == WorkspaceState.RECONCILE.value
    assert _git(repository, "rev-parse", workspace.branch) != workspace.resolved_base_commit

    _git(repository, "branch", "-f", workspace.branch, workspace.resolved_base_commit)
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
    cleaned_record = await _settled(operations, cleaned)
    assert cleaned_record.state == "succeeded"
    assert service.get(workspace.workspace_id).state == WorkspaceState.REMOVED.value
    assert _git(repository, "branch", "--list", workspace.branch) == ""

    retry = await service.reserve(request, reservation_id="branch-only-retry")
    assert retry.workspace.state == WorkspaceState.ACTIVE.value


async def test_metadata_only_creation_partial_is_not_retired_as_absent_and_can_retry(
    repository: str, workspace_services
) -> None:
    store, operations, service = workspace_services
    request = WorkspaceRequest(cwd=repository, worktree="metadata-only-partial")
    reservation = await _persist_creation_intent(
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
    _git(path, "checkout", "--detach")
    _git(repository, "branch", "-D", workspace.branch)
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
    completed = await _settled(operations, accepted)
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
    assert _git(repository, "branch", "--list", workspace.branch) == ""

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


def test_public_workspace_dto_exposes_creating_state() -> None:
    assert PublicWorkspaceState.CREATING.value == "creating"
    workspace = PublicWorkspace.from_wire(
        {
            "workspace_id": "workspace-creating",
            "ownership_kind": "theater",
            "owner_id": "theater",
            "path": "/tmp/workspace-creating",
            "state": "creating",
            "usages": [],
        }
    )
    assert workspace.state == "creating"


async def test_cleanup_recovery_reopens_only_before_dispatch(
    repository: str, workspace_services, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="reservation-recovery"
    )
    service.release_usage(reservation.usage.usage_id, reason="launch_abandoned")

    accepted = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-recovery-before-dispatch",
        params={"workspace_id": reservation.workspace.workspace_id},
    )
    await operations.aclose()
    assert (
        await service.recover_cleanup_deletion(
            reservation.workspace.workspace_id,
            operation_id=str(accepted["operation_id"]),
            non_dispatch_proven=True,
        )
        == WorkspaceState.ACTIVE.value
    )

    possible = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-recovery-possible-dispatch",
        params={"workspace_id": reservation.workspace.workspace_id},
    )
    await operations.aclose()
    original = cleanup_module.inspect_cleanup_result
    main_thread = threading.get_ident()

    def guarded(record, *, delete_branch):
        assert not store._db._write_unit_active
        assert threading.get_ident() != main_thread
        return original(record, delete_branch=delete_branch)

    monkeypatch.setattr(cleanup_module, "inspect_cleanup_result", guarded)
    assert (
        await service.recover_cleanup_deletion(
            reservation.workspace.workspace_id,
            operation_id=str(possible["operation_id"]),
            non_dispatch_proven=False,
        )
        == WorkspaceState.RECONCILE.value
    )
    assert Path(reservation.workspace.path).is_dir()


async def test_cleanup_recovery_records_partial_result_when_only_branch_remains(
    repository: str, workspace_services
) -> None:
    store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True),
        reservation_id="reservation-partial-recovery",
    )
    workspace = reservation.workspace
    service.release_usage(reservation.usage.usage_id, reason="participant_exit")
    accepted = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-partial-recovery",
        params={"workspace_id": workspace.workspace_id, "delete_branch": True},
    )
    await operations.aclose()
    operation_id = str(accepted["operation_id"])
    current = operations.get(operation_id)
    if current.state == PublicOperationState.ACCEPTED.value:
        operations.mark_running(operation_id, phase="workspace_cleanup_started")
        current = operations.get(operation_id)
    if current.state == PublicOperationState.RUNNING.value:
        operations.mark_uncertain(
            operation_id,
            phase="dispatch_cancelled",
            error={"code": "internal", "message": "restart interrupted cleanup"},
        )
    _git(repository, "worktree", "remove", "--force", workspace.path)
    assert _git(repository, "show-ref", "--verify", f"refs/heads/{workspace.branch}")

    operations.configure_reconciler(
        DurableEvidenceReconciler(store, workspace_project=service.project)
    )
    await reconcile_public_control_operations(
        SimpleNamespace(
            store=store,
            workspace_service=service,
            operation_service=operations,
        )
    )

    record = service.get(workspace.workspace_id)
    assert record.state == WorkspaceState.REMOVED.value
    assert record.cleanup_result is not None
    assert record.cleanup_result["worktree_removed"] is True
    assert record.cleanup_result["branch_removed"] is False
    assert record.cleanup_result["branch_retained"] is True
    assert record.cleanup_result["errors"] == ["requested branch deletion did not complete"]
    reconciled = operations.get(operation_id)
    assert reconciled.state == PublicOperationState.FAILED.value
    assert reconciled.phase == "workspace_cleanup_evidence_failed"


async def test_cleanup_recovery_preserves_invalid_git_facts(
    workspace_services, tmp_path: Path
) -> None:
    store, operations, service = workspace_services
    timestamp = 10.0
    workspace_id = "workspace-invalid-recovery"
    operation_id = "operation-invalid-recovery"
    with store.write_unit() as unit:
        store.workspaces.create(
            WorkspaceRecord(
                workspace_id=workspace_id,
                ownership_kind=WorkspaceOwnershipKind.THEATER.value,
                owner_id="operator-a",
                path=str(tmp_path / "missing-worktree"),
                canonical_repository_root=str(tmp_path / "missing-repository"),
                branch="missing-branch",
                state=WorkspaceState.DELETING.value,
                deletion_operation_id=operation_id,
                deletion_token="10-delete-token",
                deletion_prior_state=WorkspaceState.ACTIVE.value,
                cleanup_force=False,
                cleanup_delete_branch=True,
                cleanup_force_branch=False,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
        store.operations.create(
            PublicOperationRecord(
                operation_id=operation_id,
                kind="workspace_cleanup",
                actor_client_id="operator-a",
                actor_participant_id=None,
                target_ids=(workspace_id,),
                state=PublicOperationState.UNCERTAIN.value,
                phase="mutation_recovery_pending",
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    assert (
        await service.recover_cleanup_deletion(
            workspace_id,
            operation_id=operation_id,
            non_dispatch_proven=False,
        )
        == WorkspaceState.RECONCILE.value
    )
    recovered = service.get(workspace_id)
    assert recovered.cleanup_result is not None
    assert recovered.cleanup_result["uncertain"] is True
    assert operations.get(operation_id).state == PublicOperationState.UNCERTAIN.value
    assert (
        await reconcile_workspace_lifecycle(SimpleNamespace(store=store, workspace_service=service))
        == ()
    )


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
    failed = await _settled(operations, accepted)
    assert failed.state == "failed"
    assert service.get(workspace.workspace_id).state == "reconcile"
    assert _git(repository, "show-ref", "--verify", f"refs/heads/{workspace.branch}")


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
    failed = await _settled(operations, accepted)

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


async def test_thin_handlers_match_the_frozen_workspace_catalog(
    tmp_path: Path, workspace_services
) -> None:
    _store, _operations, service = workspace_services
    assert set(WORKSPACE_HANDLERS) == {
        name for name in METHOD_CATALOG if name.startswith("frontend.workspaces.")
    }
    context = ConnectionContext(
        client_id="operator-a",
        role=ConnectionRole.OPERATOR,
        channel=ConnectionChannel.RPC,
        api_major=1,
        api_minor=0,
        capabilities=frozenset({"workspaces.v1"}),
    )
    directory = tmp_path / "handler-workspace"
    directory.mkdir()
    result = await workspaces_register(
        SimpleNamespace(workspace_service=service),
        context,
        _external_params(directory),
        idempotency_key="handler-register",
    )
    validator_for(METHOD_CATALOG["frontend.workspaces.register"].result_schema_id).validate(result)
