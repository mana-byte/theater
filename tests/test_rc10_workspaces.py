"""Focused durable workspace lifecycle and exact Git cleanup checks."""

from __future__ import annotations

import itertools
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.workspace_handlers import WORKSPACE_HANDLERS, workspaces_register
from theater.daemon.operations import OperationService
from theater.daemon.persistence.store import Store
from theater.daemon.worktrees import cleanup as cleanup_module
from theater.daemon.worktrees import identity
from theater.daemon.worktrees.service import (
    WorkspaceDeleting,
    WorkspaceInUse,
    WorkspaceOwnershipConflict,
    WorkspaceRequest,
    WorkspaceService,
)
from theater.frontend.capabilities import METHOD_CATALOG, ConnectionChannel, ConnectionRole
from theater.frontend.schemas import validator_for
from theater.models import BadRequest


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
    assert partial.state == "succeeded", partial.error
    assert partial.result["worktree_removed"] is True
    assert partial.result["branch_retained"] is True
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
