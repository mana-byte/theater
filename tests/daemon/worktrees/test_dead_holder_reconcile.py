"""Startup reconcile releases usages stranded by a dead participant holder."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.workspace_services import make_repository, open_workspace_services
from theater.daemon.registry import Registry
from theater.daemon.worktrees.service import WorkspaceRequest
from theater.models import Status


@pytest.fixture
def repository(tmp_path: Path) -> str:
    return make_repository(tmp_path)


@pytest.fixture
def workspace_services(tmp_path: Path):
    with open_workspace_services(tmp_path) as services:
        yield services


async def test_reconcile_releases_only_dead_holder_participant_usages(
    repository: str, workspace_services
) -> None:
    store, _operations, service = workspace_services
    registry = Registry(store)

    dead = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree="pair-dead"), reservation_id="r-dead"
    )
    dead_p = registry.create_spawned(
        harness="vibe", cwd=dead.workspace.path, workspace_id=dead.workspace.workspace_id
    )
    dead_usage = service.handoff_usage(dead.usage.usage_id, participant_id=dead_p.id)
    store.set_status(dead_p.id, Status.DEAD)

    live = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree="pair-live"), reservation_id="r-live"
    )
    live_p = registry.create_spawned(
        harness="vibe", cwd=live.workspace.path, workspace_id=live.workspace.workspace_id
    )
    live_usage = service.handoff_usage(live.usage.usage_id, participant_id=live_p.id)

    # A reservation usage never handed off: not a participant holder, must be left alone.
    held = await service.reserve(
        WorkspaceRequest(cwd=repository, worktree="pair-resv"), reservation_id="r-resv"
    )

    assert service.reconcile_dead_holder_usages() == 1

    released = store.workspaces.get_usage(dead_usage.usage_id)
    assert released is not None and released.released_at is not None
    assert released.release_reason == "dead_holder_reconcile"
    assert store.workspaces.get_usage(live_usage.usage_id).released_at is None
    assert store.workspaces.get_usage(held.usage.usage_id).released_at is None

    # Idempotent: a second pass releases nothing.
    assert service.reconcile_dead_holder_usages() == 0
