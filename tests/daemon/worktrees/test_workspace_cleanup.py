"""Workspace cleanup: exact Git facts, force rules, kill cleanup and cleanup recovery."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from tests.support.provider_controls import online, provider_target
from tests.support.workspace_services import git, make_repository, open_workspace_services, settled
from theater.daemon.operations.reconciliation import DurableEvidenceReconciler
from theater.daemon.rpc.participants import terminate_participant
from theater.daemon.runtime.public_recovery import (
    reconcile_workspace_lifecycle,
)
from theater.daemon.runtime.recovery import reconcile_public_control_operations
from theater.daemon.schema import orchestration_events
from theater.daemon.worktrees import cleanup as cleanup_module
from theater.daemon.worktrees.service import (
    WorkspaceRequest,
)
from theater.models import (
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
    failed = await settled(operations, refused)
    assert failed.state == "failed"
    assert service.get(reservation.workspace.workspace_id).state == "reconcile"
    assert dirty.exists()

    forced = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-dirty-force",
        params={"workspace_id": reservation.workspace.workspace_id, "force": True},
    )
    succeeded = await settled(operations, forced)
    assert succeeded.state == "succeeded"
    assert succeeded.result["branch_retained"] is True
    assert not Path(reservation.workspace.path).exists()


@pytest.mark.parametrize("contents", ["clean", "dirty", "unmerged", "moved", "shared", "in_use"])
async def test_kill_cleans_only_unused_unique_workspaces(daemon, repository, monkeypatch, contents):
    participant_id = provider_target(daemon)
    online(monkeypatch, daemon)
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
        git(workspace.path, "add", "work.txt")
        git(workspace.path, "commit", "-m", "child work")
    if contents == "moved":
        git(workspace.path, "checkout", "-b", "child-switched")

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
    retained = contents in {"dirty", "moved", "shared", "in_use"}
    assert Path(workspace.path).exists() is retained
    assert (workspace.path in git(repository, "worktree", "list", "--porcelain")) is retained
    assert bool(git(repository, "branch", "--list", workspace.branch)) is (contents != "clean")
    if contents == "shared":
        assert "workspace_cleanup" not in result
    else:
        assert result["workspace_cleanup"]["state"] == (
            "succeeded"
            if contents in {"clean", "unmerged"}
            else "retained"
            if contents in {"in_use", "moved"}
            else "failed"
        )
    if contents == "moved":
        assert result["workspace_cleanup"]["reason"] == "branch_moved"
        assert result["workspace_cleanup"]["next"].endswith("--force")
    if contents == "dirty":
        assert work.read_text() == "retain this work\n"
    if contents == "unmerged":
        assert git(repository, "show", f"{workspace.branch}:work.txt") == "retain this work"


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
    assert (await settled(operations, first)).state == PublicOperationState.SUCCEEDED.value
    git(repository, "branch", "-D", "--", workspace.branch)

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
    await settled(operations, accepted)


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
    git(workspace.path, "add", "result.txt")
    git(workspace.path, "commit", "-m", "unmerged result")

    retained = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-retain-unmerged",
        params={"workspace_id": workspace.workspace_id, "delete_branch": True},
    )
    partial = await settled(operations, retained)
    assert partial.state == "succeeded"
    assert partial.result["worktree_removed"] is True
    assert partial.result["branch_retained"] is True
    assert partial.result["reason"] == "unmerged"
    assert git(repository, "show-ref", "--verify", f"refs/heads/{workspace.branch}")

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
    completed = await settled(operations, forced)
    assert completed.result["branch_removed"] is True


async def test_moved_branch_cleanup_requires_force_and_keeps_both_branches(
    repository: str, workspace_services
) -> None:
    _store, operations, service = workspace_services
    reservation = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree=True), reservation_id="reservation-moved"
    )
    workspace = reservation.workspace
    service.release_usage(reservation.usage.usage_id, reason="participant_exit")
    git(workspace.path, "checkout", "-b", "child-switched")

    refused = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-moved-refused",
        params={"workspace_id": workspace.workspace_id, "delete_branch": True},
    )
    failed = await settled(operations, refused)
    assert failed.state == "failed"
    assert failed.error is not None
    details = failed.error["details"]
    assert details["reason"] == "branch_moved"
    assert details["recorded_branch"] == workspace.branch
    assert details["current_branch"] == "child-switched"
    assert details["next"] == f"theater workspaces cleanup {workspace.workspace_id} --force"
    assert service.get(workspace.workspace_id).cleanup_result["reason"] == "branch_moved"

    forced = service.cleanup(
        client_id="operator-a",
        actor_participant_id=None,
        idempotency_key="cleanup-moved-force",
        params={"workspace_id": workspace.workspace_id, "force": True, "delete_branch": True},
    )
    succeeded = await settled(operations, forced)
    assert succeeded.state == "succeeded"
    assert not Path(workspace.path).exists()
    assert git(repository, "show-ref", "--verify", "refs/heads/child-switched")
    # The recorded branch was merged (never advanced), so the exact -d rule deleted it.
    assert succeeded.result["branch_removed"] is True


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
    git(repository, "worktree", "remove", "--force", workspace.path)
    assert git(repository, "show-ref", "--verify", f"refs/heads/{workspace.branch}")

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
