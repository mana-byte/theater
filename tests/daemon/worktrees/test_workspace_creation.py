"""Workspace registration, ownership fences, usage tracking and named-workspace creation."""

from __future__ import annotations

import asyncio
import itertools
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from tests.support.workspace_services import (
    external_params,
    git,
    make_repository,
    open_workspace_services,
    settled,
)
from theater.daemon.frontend.handshake import ConnectionContext
from theater.daemon.frontend.workspace_handlers import WORKSPACE_HANDLERS, workspaces_register
from theater.daemon.operations import OperationService
from theater.daemon.persistence.store import Store
from theater.daemon.schema import workspaces as workspaces_table
from theater.daemon.worktrees.paths import named_branch_name, named_worktree_path
from theater.daemon.worktrees.service import (
    WorkspaceDeleting,
    WorkspaceInUse,
    WorkspaceOwnershipConflict,
    WorkspaceRequest,
    WorkspaceService,
)
from theater.frontend.capabilities import METHOD_CATALOG, ConnectionChannel, ConnectionRole
from theater.frontend.dto.workspaces import Workspace as PublicWorkspace
from theater.frontend.dto.workspaces import WorkspaceState as PublicWorkspaceState
from theater.frontend.schemas import validator_for
from theater.models import (
    BadRequest,
)


@pytest.fixture
def repository(tmp_path: Path) -> str:
    return make_repository(tmp_path)


@pytest.fixture
def workspace_services(tmp_path: Path):
    with open_workspace_services(tmp_path) as services:
        yield services


async def test_borrowing_an_owned_path_names_its_live_holder(
    tmp_path: Path, workspace_services
) -> None:
    _store, _operations, service = workspace_services
    directory = tmp_path / "frontend-workspace"
    directory.mkdir()
    registered = await service.register(
        client_id="operator-a", idempotency_key="register-a", params=external_params(directory)
    )
    reservation = await service.reserve(
        WorkspaceRequest(workspace_id=str(registered["workspace_id"])), reservation_id="r-a"
    )
    service.handoff_usage(reservation.usage.usage_id, participant_id="participant-a")
    with pytest.raises(WorkspaceOwnershipConflict, match="owned by live participant participant-a"):
        await service.reserve(WorkspaceRequest(cwd=str(directory)), reservation_id="r-b")


async def test_external_deletion_fence_serializes_usage_and_exact_token(
    tmp_path: Path, workspace_services
) -> None:
    store, _operations, service = workspace_services
    directory = tmp_path / "frontend-workspace"
    directory.mkdir()
    params = external_params(directory)
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
        params=external_params(directory),
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
    params = external_params(directory)
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
    git(repository, "worktree", "add", "-b", "initiating", str(linked), "HEAD")
    (linked / "linked.txt").write_text("linked head\n")
    git(linked, "add", "linked.txt")
    git(linked, "commit", "-m", "linked head")
    linked_head = git(linked, "rev-parse", "HEAD")
    assert linked_head != git(repository, "rev-parse", "HEAD")

    reservation = await service.reserve(
        WorkspaceRequest(cwd=str(linked), worktree=True),
        reservation_id="reservation-linked",
    )

    workspace = reservation.workspace
    assert workspace.canonical_repository_root == repository
    assert workspace.resolved_base_commit == linked_head
    assert git(workspace.path, "rev-parse", "HEAD") == linked_head


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
    completed = await settled(operations, accepted)
    assert completed.result["worktree_removed"] is True
    assert completed.result["branch_retained"] is True

    with pytest.raises(BadRequest, match="already exists"):
        await service.reserve(
            WorkspaceRequest(cwd=repository, worktree="shared"),
            reservation_id="reservation-named-c",
        )


async def test_named_workspace_uses_canonical_repository_and_resolved_base(
    repository: str, workspace_services
) -> None:
    _store, _operations, service = workspace_services
    linked = Path(repository).parent / "linked-source"
    git(repository, "worktree", "add", "-b", "linked-source", str(linked), "HEAD")
    first = await service.reserve(
        WorkspaceRequest(cwd=str(linked), worktree="shared-linked", base_ref="main"),
        reservation_id="linked-first",
    )
    assert first.workspace.canonical_repository_root == repository
    assert first.workspace.path.startswith(repository)
    joined = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree="shared-linked", base_ref="HEAD"),
        reservation_id="linked-join",
    )
    assert joined.workspace.workspace_id == first.workspace.workspace_id

    git(repository, "checkout", "-b", "different")
    (Path(repository) / "different.txt").write_text("different\n")
    git(repository, "add", "different.txt")
    git(repository, "commit", "-m", "different")
    with pytest.raises(WorkspaceOwnershipConflict, match="named_base_mismatch"):
        await service.reserve(
            WorkspaceRequest(cwd=repository, worktree="shared-linked", base_ref="different"),
            reservation_id="linked-conflict",
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("missing", "does not exist"),
        ("checked_out_branch", "checked out"),
        ("persisted_path", "expected Theater-managed path"),
        ("persisted_branch", "expected Theater-managed branch"),
    ],
)
async def test_named_workspace_join_refuses_changed_identity(
    repository: str, workspace_services, change: str, message: str
) -> None:
    store, _operations, service = workspace_services
    first = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree="identity"), reservation_id="identity-first"
    )
    workspace = first.workspace
    service.release_usage(first.usage.usage_id, reason="seed_complete")
    if change == "missing":
        shutil.rmtree(workspace.path)
    elif change == "checked_out_branch":
        git(workspace.path, "checkout", "-b", "hijacked")
    else:
        column = (
            workspaces_table.c.path if change == "persisted_path" else workspaces_table.c.branch
        )
        with store.write_unit() as unit:
            unit.connection.execute(
                update(workspaces_table)
                .where(workspaces_table.c.workspace_id == workspace.workspace_id)
                .values({column.key: "wrong"})
            )
    with pytest.raises(BadRequest, match=message):
        await service.reserve(
            WorkspaceRequest(cwd=repository, worktree="identity"), reservation_id="identity-join"
        )


async def test_concurrent_named_workspace_creation_has_one_durable_owner(
    repository: str, workspace_services
) -> None:
    _store, _operations, service = workspace_services
    request = WorkspaceRequest(cwd=repository, worktree="concurrent")
    results = await asyncio.gather(
        service.reserve(request, reservation_id="concurrent-a"),
        service.reserve(request, reservation_id="concurrent-b"),
        return_exceptions=True,
    )
    created = [result for result in results if not isinstance(result, BaseException)]
    assert created
    assert {result.workspace.workspace_id for result in created} == {
        created[0].workspace.workspace_id
    }
    failures = [result for result in results if isinstance(result, BaseException)]
    assert all(isinstance(result, (WorkspaceOwnershipConflict, BadRequest)) for result in failures)
    joined = await service.reserve(request, reservation_id="concurrent-retry")
    assert joined.workspace.workspace_id == created[0].workspace.workspace_id


async def test_historical_named_worktree_row_survives_new_workspace_reservation(
    repository: str, tmp_path: Path
) -> None:
    with open_workspace_services(tmp_path) as (store, _operations, service):
        store.upsert_named_worktree(
            repo_root=repository,
            name="legacy",
            branch=named_branch_name("legacy"),
            path=named_worktree_path(repository, "legacy"),
            base_branch=None,
        )
        await service.reserve(
            WorkspaceRequest(cwd=repository, worktree="current"), reservation_id="current-first"
        )
    reopened = Store(tmp_path / "workspaces.db")
    try:
        row = reopened.get_named_worktree(repo_root=repository, name="legacy")
        assert row is not None and row["created_at"] > 0
        assert {key: value for key, value in row.items() if key != "created_at"} == {
            "repo_root": repository,
            "name": "legacy",
            "branch": named_branch_name("legacy"),
            "path": named_worktree_path(repository, "legacy"),
            "base_branch": None,
        }
    finally:
        reopened.close()


async def test_named_workspace_joins_after_service_restart(repository: str, tmp_path: Path) -> None:
    request = WorkspaceRequest(cwd=repository, worktree="restart-join")
    with open_workspace_services(tmp_path) as (_store, _operations, service):
        first = await service.reserve(request, reservation_id="restart-first")
        workspace_id = first.workspace.workspace_id
        path = first.workspace.path
    with open_workspace_services(tmp_path) as (_store, _operations, service):
        joined = await service.reserve(request, reservation_id="restart-second")
        assert joined.workspace.workspace_id == workspace_id
        assert joined.workspace.path == path


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
        external_params(directory),
        idempotency_key="handler-register",
    )
    validator_for(METHOD_CATALOG["frontend.workspaces.register"].result_schema_id).validate(result)
