"""Killing and forgetting participants: who may, what it releases, and what jobs end as."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy import insert

from tests.support.presence import FakePresence
from theater.constants.refusals import NEXT_FORGET
from theater.daemon.schema import launch_reservations, public_operations
from theater.models import Status
from theater.protocol import RemoteError


async def test_kill_marks_dead_and_hides(client, terminal_provider):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])

    assert await client.call("participants.list") == []
    dead = await client.call("participants.get", id=record["id"])
    assert dead["status"] == "dead"
    assert dead["addressable"] is False


async def test_kill_from_a_caller_who_is_the_parent_succeeds(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    result = await client.call("participant.kill", id=child["id"], caller_id=parent["id"])
    assert result == {"id": child["id"], "killed": True}
    dead = await client.call("participants.get", id=child["id"])
    assert dead["status"] == "dead"


async def test_kill_refuses_a_target_that_is_not_the_callers_child(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    stranger = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
    )
    with pytest.raises(RemoteError) as exc:
        await client.call("participant.kill", id=stranger["id"], caller_id=parent["id"])
    assert exc.value.code == "not_your_child"
    alive = await client.call("participants.get", id=stranger["id"])
    assert alive["status"] != "dead"


async def test_kill_refuses_self_kill(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("participant.kill", id=parent["id"], caller_id=parent["id"])
    assert exc.value.code == "no_self_kill"
    alive = await client.call("participants.get", id=parent["id"])
    assert alive["status"] != "dead"


async def test_kill_on_an_already_dead_child_is_a_no_op(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])
    result = await client.call("participant.kill", id=child["id"], caller_id=parent["id"])
    assert result == {"id": child["id"], "killed": False, "reason": "already_dead"}


async def test_kill_without_caller_id_is_unrestricted(client, terminal_provider, daemon):
    """The CLI and the régie send no caller_id; a human may kill anything."""
    daemon.presence = FakePresence()
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    stranger = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
    )
    result = await client.call("participant.kill", id=stranger["id"])
    assert result == {"id": stranger["id"], "killed": True}
    result = await client.call("participant.kill", id=parent["id"])
    assert result == {"id": parent["id"], "killed": True}


async def test_kill_finishes_running_jobs_as_killed(client, terminal_provider):
    """A child killed mid-job must end KILLED, not stranded RUNNING.

    Before the fix, _kill never touched jobs: the job row stayed RUNNING
    forever and the parent's await_sessions never woke. The kill path now
    finishes every still-running job targeting the killed participant with
    state KILLED after spawner.kill_pane succeeds.
    """
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="do some work",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle = child["handle"]

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "running"

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "killed"
    assert job["error_code"] == "killed"


async def test_kill_wakes_the_awaiter_immediately(client, terminal_provider):
    """The job is terminal the moment the kill returns, not after a reaper tick.

    await_sessions returns the current job state; a KILLED job must read as
    terminal right away so the parent is not blocked until the reaper runs.
    """
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="do some work",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle = child["handle"]

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    jobs = await client.call("jobs.await", handles=[handle], max_wait=1.0)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "killed"


async def test_kill_finishes_jobs_before_releasing_workspace_usage(
    daemon, client, terminal_provider, monkeypatch
):
    """Jobs finish before durable workspace usage is released.

    Job completion hashes files in the worktree to record ``sha_after``; if
    usage were released first, explicit cleanup could remove the files before
    their final hashes are recorded.
    """
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="do some work",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle = child["handle"]

    order: list[str] = []

    original_finish = daemon.jobs.finish

    def spy_finish(*args, **kwargs):
        order.append("finish")
        return original_finish(*args, **kwargs)

    original_release = daemon.spawner.release_workspace_usage

    def spy_release(p, *, reason):
        order.append("release")
        return original_release(p, reason=reason)

    monkeypatch.setattr(daemon.jobs, "finish", spy_finish)
    monkeypatch.setattr(daemon.spawner, "release_workspace_usage", spy_release)

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "killed"
    assert order.index("finish") < order.index("release")


def _make_repo(tmp_path):
    """A real git repo with one commit, for worktree tests."""
    import subprocess

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return str(root)


async def test_kill_of_worktree_child_preserves_non_null_sha_after(
    daemon, client, terminal_provider, tmp_path
):
    """A worktree child killed mid-job must record real sha_after, not NULL.

    The touch table records sha_after by hashing files in the child's worktree
    at job-finish time. If the worktree directory is deleted before the job
    finishes, every path reads as gone and every row gets sha_after=NULL — a
    spurious deletion. This end-to-end test uses a real git repo and worktree
    and asserts the touch row carries a real hash.
    """
    from theater.daemon.schema import touch as touch_table
    from theater.harness.base import EventPath

    repo_root = _make_repo(tmp_path)

    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="do some work",
        approval="manual",
        cwd=repo_root,
        parent_id=parent["id"],
        worktree=True,
    )
    handle = child["handle"]

    # The worktree directory is the child's cwd. Write a file there and feed
    # it to the accumulator, the way the observer would for an event carrying
    # Event.paths.
    wt_cwd = child["cwd"]
    (Path(wt_cwd) / "touched.py").write_bytes(b"content")

    daemon.jobs.observe_paths(handle, (EventPath(path="touched.py", mode="write"),))

    await client.call("participant.kill", id=child["id"], caller_id=parent["id"])

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "killed"

    rows = list(daemon.store.conn.execute(touch_table.select()))
    assert len(rows) == 1
    row = rows[0]._mapping
    assert row["path"] == "touched.py"
    # sha_after must not be NULL — the worktree existed when finish ran.
    assert row["sha_after"] is not None
    assert Path(wt_cwd).is_dir(), "participant exit must retain the worktree"


def _orphan(daemon, name: str | None = None):
    """A spawned child with no binding, whose parent and owner are dead."""
    parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    child = daemon.registry.create_spawned(
        harness="vibe", cwd="/tmp", parent_id=parent.id, name=name
    )
    daemon.registry.mark_dead(parent.id)
    return child


def _uncertain_spawn(daemon, participant_id: str) -> None:
    with daemon.store.write_unit() as unit:
        unit.connection.execute(
            insert(public_operations).values(
                operation_id="op-spawn",
                kind="spawn",
                actor_client_id="c",
                target_ids=json.dumps([participant_id]),
                state="uncertain",
                phase="terminal_create_outcome_unknown",
                created_at=1.0,
                updated_at=1.0,
            )
        )
        unit.connection.execute(
            insert(launch_reservations).values(
                operation_id="op-spawn",
                participant_id=participant_id,
                provider_id="provider-fixture",
                adapter="vibe",
                phase="terminal_create_dispatched",
                launch_facts="{}",
                artifact_refs="[]",
                created_at=1.0,
                updated_at=1.0,
            )
        )


async def test_any_live_participant_forgets_an_unbound_orphan(client, daemon):
    child = _orphan(daemon, "ghost")
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    daemon.jobs.create(handle="s-1", caller_id=stranger.id, target_id=child.id, kind="send")

    # Without forget the presence gate can never pass, and next says so.
    with pytest.raises(RemoteError) as refused:
        await client.call("participant.kill", id=child.id)
    assert refused.value.code == "human_present"
    assert refused.value.details["next"] == NEXT_FORGET

    result = await client.call("participant.kill", id=child.id, caller_id=stranger.id, forget=True)
    assert result == {"id": child.id, "killed": True, "forgotten": True}
    row = daemon.store.get_participant(child.id)
    assert (row.status, row.termination_reason) == (Status.DEAD, "forgotten")
    assert daemon.registry.projection_name(child.id) is None
    job = daemon.store.get_job("s-1")
    assert (job.state, job.result) == ("killed", "target forgotten")


async def test_forget_refuses_a_bound_or_live_participant(client, daemon, terminal_provider):
    bound = _orphan(daemon)
    terminal_provider.bind(daemon, bound.id)
    live = _orphan(daemon)
    live.pid = os.getpid()
    daemon.store.upsert_participant(live)

    for target, needle in ((bound.id, "terminal binding"), (live.id, "is alive")):
        with pytest.raises(RemoteError) as refused:
            await client.call("participant.kill", id=target, forget=True)
        assert refused.value.code == "bad_request"
        assert needle in refused.value.message
        assert daemon.store.get_participant(target).status is not Status.DEAD


async def test_only_the_operator_forgets_an_uncertain_spawn(client, daemon):
    child = _orphan(daemon)
    _uncertain_spawn(daemon, child.id)
    stranger = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")

    with pytest.raises(RemoteError) as refused:
        await client.call("participant.kill", id=child.id, caller_id=stranger.id, forget=True)
    assert "only the local operator" in refused.value.message

    result = await client.call("participant.kill", id=child.id, forget=True)
    assert result["forgotten"] is True
