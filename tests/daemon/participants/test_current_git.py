"""participants.get include_git: the cwd's Git state now, beside the recorded branch."""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest

from tests.support.workspace_services import git, make_repository, persist_creation_intent
from theater.daemon.handoff import gitfacts
from theater.daemon.rpc import participants as participants_rpc
from theater.daemon.worktrees import repository
from theater.daemon.worktrees.service import WorkspaceRequest
from theater.protocol import RemoteError


async def test_reused_borrowed_record_keeps_its_branch_while_git_reports_the_checkout(
    client, daemon, tmp_path
):
    repo = make_repository(tmp_path)
    git(repo, "checkout", "-q", "-b", "rc10/integration")
    first = await persist_creation_intent(
        daemon.store, daemon.workspace_service, WorkspaceRequest(cwd=repo), reservation_id="r1"
    )
    git(repo, "checkout", "-q", "main")
    reused = await persist_creation_intent(
        daemon.store, daemon.workspace_service, WorkspaceRequest(cwd=repo), reservation_id="r2"
    )
    assert (reused.created, reused.workspace.workspace_id) == (False, first.workspace.workspace_id)
    assert reused.workspace.branch == "rc10/integration"
    workspace_id = reused.workspace.workspace_id
    # As spawn admission does: the participant copies the reused record's branch.
    participant = daemon.registry.create_spawned(
        harness="vibe", cwd=repo, workspace_id=workspace_id
    )
    participant.branch = reused.workspace.branch
    daemon.store.upsert_participant(participant)
    await client.call("hello", id=participant.id, harness="vibe", cwd=repo)

    plain = await client.call("participants.get", id=participant.id)
    got = await client.call("participants.get", id=participant.id, include_git=True)

    assert "git" not in plain
    assert got["branch"] == plain["branch"] == "rc10/integration"
    assert got["git"] == {
        "pin_state": "pinned",
        "commit_sha": git(repo, "rev-parse", "HEAD"),
        "branch": "main",
        "dirty_entries": 0,
        "reason": None,
    }
    assert daemon.store.get_participant(participant.id).branch == "rc10/integration"
    assert daemon.store.workspaces.get(workspace_id).branch == "rc10/integration"


async def test_git_is_read_off_the_loop_and_only_when_asked(client, daemon, tmp_path, monkeypatch):
    threads: list[str] = []

    def recording(cwd):
        threads.append(threading.current_thread().name)
        return gitfacts.pin_git_facts(cwd)

    monkeypatch.setattr(participants_rpc, "pin_git_facts", recording)
    me = await client.call("hello", harness="vibe", cwd=make_repository(tmp_path))

    await client.call("participants.list")
    await client.call("participants.tree")
    await client.call("participants.get", id=me["id"], self_view=True)
    assert threads == []

    await client.call("participants.get", id=me["id"], include_git=True)
    assert len(threads) == 1 and threads[0].startswith("theater-worker")

    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get", id=me["id"], include_git="yes")
    assert exc.value.code == "bad_request"


@pytest.mark.parametrize("case", ["detached", "missing", "not_git", "timeout"])
async def test_unreadable_or_detached_checkouts_answer_without_failing(
    client, daemon, tmp_path, monkeypatch, case
):
    repo = make_repository(tmp_path)
    cwd = {"missing": str(tmp_path / "gone"), "not_git": str(tmp_path)}.get(case, repo)
    if case == "detached":
        git(repo, "checkout", "-q", "--detach")
    if case == "timeout":

        def timing_out(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

        # Only Git's module sees the timeout; the daemon's other subprocesses run normally.
        fake = SimpleNamespace(**{**vars(subprocess), "run": timing_out})
        monkeypatch.setattr(repository, "subprocess", fake)
    me = await client.call("hello", harness="vibe", cwd=cwd)

    got = (await client.call("participants.get", id=me["id"], include_git=True))["git"]

    expected = {
        "detached": ("pinned", None, None),
        "missing": ("unpinned", None, "workspace_missing"),
        "not_git": ("unpinned", None, "git_unavailable"),
        "timeout": ("unpinned", None, "git_unavailable"),
    }[case]
    assert (got["pin_state"], got["branch"], got["reason"]) == expected
    assert (got["commit_sha"] is not None) is (case == "detached")
