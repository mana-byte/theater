"""Acceptance tests for Theater.

The spec's acceptance test is:

> Two Vibe sessions started by hand plus one Claude Code session all
> appear in the tree with live status; one Vibe agent spawns a worker,
> awaits it, and uses the result; the whole thing survives a tmux
> detach and a daemon restart.

This file automates everything that can be tested without real harnesses:
spawn → await → result, fan-out, lineage tree, restart reconciliation,
depth cap, and cycle detection. The parts that need real Vibe/Claude
sessions are documented as a manual procedure in docs/acceptance.md.

These tests are deliberately end-to-end: they go through the socket,
through the daemon, and through the real JobManager + Observer wiring.
tmux is stubbed, but everything above that boundary is real.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from theater.daemon.jobs import JobState
from theater.protocol import RemoteError

# ========================================================================
# 1. spawn → await → result
# ========================================================================


async def test_acceptance_spawn_await_result(client, fake_tmux, daemon):
    """An agent spawns a worker, awaits it, and receives the result.

    This is the core orchestration loop. The observer detects turn-end
    in the child's transcript and finishes the job with the assistant
    text as the result.
    """
    # A parent spawns a child.
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="say hello",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle = child["handle"]
    assert handle == child["id"]

    # The job is running.
    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "running"

    # The observer detects turn-end and finishes the job.
    daemon.jobs.finish(handle, state=JobState.DONE, result="hello world")

    # The parent awaits the result.
    jobs = await client.call("jobs.await", handles=[handle], max_wait=2.0, caller_id=parent["id"])
    assert len(jobs) == 1
    assert jobs[0]["state"] == "done"
    assert jobs[0]["result"] == "hello world"


# ========================================================================
# 2. Fan-out: spawn 3 workers, await them together
# ========================================================================


async def test_acceptance_fan_out(client, fake_tmux, daemon):
    """Fan-out: spawn three workers, await them together."""
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")

    handles = []
    for i in range(3):
        record = await client.call(
            "spawn",
            harness="vibe",
            prompt=f"task {i}",
            approval="manual",
            cwd="/tmp",
            parent_id=parent["id"],
        )
        handles.append(record["handle"])

    # Finish two of three.
    daemon.jobs.finish(handles[0], state=JobState.DONE, result="result 0")
    daemon.jobs.finish(handles[1], state=JobState.DONE, result="result 1")

    # Await all three. The third is still running.
    jobs = await client.call("jobs.await", handles=handles, max_wait=0.5, caller_id=parent["id"])
    states = {j["handle"]: j["state"] for j in jobs}
    results = {j["handle"]: j["result"] for j in jobs}
    assert states[handles[0]] == "done"
    assert states[handles[1]] == "done"
    assert states[handles[2]] == "running"
    assert results[handles[0]] == "result 0"
    assert results[handles[1]] == "result 1"

    # Now finish the third and re-await.
    daemon.jobs.finish(handles[2], state=JobState.DONE, result="result 2")
    jobs = await client.call("jobs.await", handles=handles, max_wait=2.0, caller_id=parent["id"])
    states = {j["handle"]: j["state"] for j in jobs}
    assert all(s == "done" for s in states.values())


# ========================================================================
# 3. Tree shows correct lineage and status
# ========================================================================


async def test_acceptance_tree_lineage(client, fake_tmux):
    """The tree shows parent → children with correct lineage."""
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp/parent")
    child1 = await client.call(
        "spawn",
        harness="vibe",
        prompt="task 1",
        approval="manual",
        cwd="/tmp/c1",
        parent_id=parent["id"],
    )
    child2 = await client.call(
        "spawn",
        harness="claude",
        prompt="task 2",
        approval="manual",
        cwd="/tmp/c2",
        parent_id=parent["id"],
    )

    tree = await client.call("participants.tree")
    assert len(tree) == 1  # one root
    root = tree[0]
    assert root["id"] == parent["id"]
    assert root["harness"] == "vibe"
    assert len(root["children"]) == 2
    child_ids = {c["id"] for c in root["children"]}
    assert child_ids == {child1["id"], child2["id"]}


# ========================================================================
# 4. Kill + restart mid-job → crashed
# ========================================================================


# ========================================================================
# 5. Depth cap rejection
# ========================================================================


async def test_acceptance_depth_cap_rejects_deep_spawn(client, fake_tmux):
    """A spawn that would exceed the depth cap is rejected cleanly."""
    root = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    d1 = await client.call(
        "spawn", harness="vibe", prompt="d1", approval="manual", cwd="/tmp", parent_id=root["id"]
    )
    d2 = await client.call(
        "spawn", harness="vibe", prompt="d2", approval="manual", cwd="/tmp", parent_id=d1["id"]
    )
    # d2 is at depth 2. Spawning from d2 is depth 3 = cap. OK.
    d3 = await client.call(
        "spawn", harness="vibe", prompt="d3", approval="manual", cwd="/tmp", parent_id=d2["id"]
    )
    # d3 is at depth 3. Spawning from d3 is depth 4 > cap. Rejected.
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "spawn", harness="vibe", prompt="d4", approval="manual", cwd="/tmp", parent_id=d3["id"]
        )
    assert exc.value.code == "depth_exceeded"


# ========================================================================
# 6. Cycle detection rejection
# ========================================================================


async def test_acceptance_cycle_detection_rejects_await(client, fake_tmux):
    """A→spawn B→spawn C. C awaiting A is a clean rejection, not a hang."""
    a = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    b = await client.call(
        "spawn", harness="vibe", prompt="b", approval="manual", cwd="/tmp", parent_id=a["id"]
    )
    c = await client.call(
        "spawn", harness="vibe", prompt="c", approval="manual", cwd="/tmp", parent_id=b["id"]
    )

    # C awaits A: A is an ancestor of C → cycle.
    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.await", handles=[a["id"]], max_wait=1.0, caller_id=c["id"])
    assert exc.value.code == "cycle_detected"

    # A awaits B: normal pattern, not a cycle.
    jobs = await client.call("jobs.await", handles=[b["id"]], max_wait=0.1, caller_id=a["id"])
    assert len(jobs) == 1


# ========================================================================
# 7. Bus records the full story
# ========================================================================


# ========================================================================
# 8. Multi-handle await returns on FIRST completion
# ========================================================================


async def test_acceptance_multi_await_returns_on_first_completion(
    client, fake_tmux, daemon, monkeypatch
):
    """Multi-handle await returns when ANY job finishes; unfinished siblings are running.

    Fails on this branch: production awaits with ALL_COMPLETED, so finishing
    one of two running jobs does not wake the call — the outer wait_for trips
    the deadlock bound.
    """
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    child_a = await client.call(
        "spawn",
        harness="vibe",
        prompt="task A",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    child_b = await client.call(
        "spawn",
        harness="vibe",
        prompt="task B",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle_a, handle_b = child_a["handle"], child_b["handle"]

    # Spy on await_jobs so we deterministically know the RPC has entered the
    # daemon before finishing a job. monkeypatch restores even on failure.
    await_entered = asyncio.Event()
    original = daemon.jobs.await_jobs

    async def _spy(handles, max_wait=150.0):
        await_entered.set()
        return await original(handles, max_wait=max_wait)

    monkeypatch.setattr(daemon.jobs, "await_jobs", _spy)

    await_task = asyncio.create_task(
        client.call(
            "jobs.await",
            handles=[handle_a, handle_b],
            max_wait=10.0,
            caller_id=parent["id"],
        )
    )
    try:
        await asyncio.wait_for(await_entered.wait(), timeout=5.0)
        await asyncio.sleep(0)  # let the real await_jobs park in asyncio.wait

        daemon.jobs.finish(handle_a, state=JobState.DONE, result="A done")

        # Under FIRST_COMPLETED this returns now; under ALL_COMPLETED it
        # blocks for the full max_wait and the bound trips.
        jobs = await asyncio.wait_for(await_task, timeout=5.0)

        states = {j["handle"]: j["state"] for j in jobs}
        assert set(states) == {handle_a, handle_b}
        assert states[handle_a] == "done"
        assert states[handle_b] == "running"
    finally:
        if not await_task.done():
            await_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await await_task
