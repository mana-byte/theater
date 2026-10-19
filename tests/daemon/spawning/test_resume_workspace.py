"""A resume over RPC rejoins its predecessor's THEATER workspace instead of borrowing its path."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.workspace_services import make_repository
from theater.daemon.worktrees.service import WorkspaceRequest
from theater.harness import HARNESSES
from theater.harness.base import ResumeLaunchOverlay
from theater.protocol import RemoteError


@pytest.fixture
def lenient_resume(monkeypatch, terminal_provider):
    """Skip vibe's transcript-domain proof; only the workspace path is under test."""
    vibe = HARNESSES["vibe"]
    monkeypatch.setattr(type(vibe), "resume_preflight", lambda *_, **__: None)
    monkeypatch.setattr(type(vibe), "resume_launch_overlay", lambda *_, **__: ResumeLaunchOverlay())


async def _dead_predecessor(daemon, workspace):
    p = daemon.registry.create_spawned(
        harness="vibe", cwd=workspace.path, workspace_id=workspace.workspace_id
    )
    p.session_id = "sess-abc"
    p.session_correlation = "exact"
    daemon.registry.store.upsert_participant(p)
    daemon.registry.mark_dead(p.id)
    return p


async def _resume(client, predecessor_id: str, cwd: str):
    return await client.call(
        "spawn",
        harness="vibe",
        approval="manual",
        cwd=cwd,
        resume=predecessor_id,
    )


async def test_resume_joins_the_predecessors_named_worktree(
    lenient_resume, daemon, client, tmp_path: Path
):
    repo = make_repository(tmp_path)
    workspace = (
        await daemon.workspace_service.reserve(
            WorkspaceRequest(cwd=repo, worktree="pair-x"), reservation_id="r-builder"
        )
    ).workspace
    predecessor = await _dead_predecessor(daemon, workspace)

    record = await _resume(client, predecessor.id, workspace.path)

    successor = daemon.registry.store.get_participant(record["id"])
    assert successor.workspace_id == workspace.workspace_id
    holders = {u.holder_id for u in daemon.store.workspaces.active_usages(workspace.workspace_id)}
    assert {"r-builder", successor.id} <= holders


async def test_refused_resume_leaves_no_participant_or_usage(
    lenient_resume, daemon, client, tmp_path: Path
):
    repo = make_repository(tmp_path)
    own = (
        await daemon.workspace_service.reserve(
            WorkspaceRequest(cwd=repo, worktree="pair-own"), reservation_id="r-own"
        )
    ).workspace
    other = (
        await daemon.workspace_service.reserve(
            WorkspaceRequest(cwd=repo, worktree="pair-other"), reservation_id="r-other"
        )
    ).workspace
    predecessor = await _dead_predecessor(daemon, own)
    before = len(daemon.registry.list(include_dead=True))

    with pytest.raises(RemoteError, match="path_already_owned"):
        await _resume(client, predecessor.id, other.path)

    assert len(daemon.registry.list(include_dead=True)) == before
    assert [u.holder_id for u in daemon.store.workspaces.active_usages(other.workspace_id)] == [
        "r-other"
    ]
