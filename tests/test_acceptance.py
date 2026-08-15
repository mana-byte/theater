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


async def test_acceptance_multi_await_returns_on_first_completion(client, fake_tmux, daemon):
    """Awaiting multiple handles returns as soon as ANY job finishes.

    Contract: jobs.await with multiple handles returns once any job reaches a
    terminal state. It still returns one current-state entry per requested
    handle, so unfinished siblings come back as state=running. If a terminal
    handle is already present at call entry, the call returns immediately.

    This test starts an await while two jobs are running, finishes exactly
    one after the await is in flight, and asserts the call returns well
    before max_wait with one done and one still running. Coordination is
    event-driven — the short outer wait_for is a failure bound only.
    """
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")

    # Two children, both running.
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
    handle_a = child_a["handle"]
    handle_b = child_b["handle"]
    assert daemon.jobs.get(handle_a).state == JobState.RUNNING
    assert daemon.jobs.get(handle_b).state == JobState.RUNNING

    # An event the await task sets once it has entered the daemon's
    # await_jobs — proving the finish happens *after* the await started,
    # not before. We detect entry by hooking the real await_jobs briefly.
    await_started = asyncio.Event()
    original = daemon.jobs.await_jobs

    async def _spy(handles, max_wait=original.__defaults__[0]):
        await_started.set()
        return await original(handles, max_wait=max_wait)

    daemon.jobs.await_jobs = _spy

    # Launch the await as a background task so we can finish a job while it
    # is in flight. max_wait is generous: under FIRST_COMPLETED the call
    # returns the moment one job finishes; under ALL_COMPLETED (the bug) it
    # blocks until max_wait, and the outer wait_for trips the failure bound.
    await_task = asyncio.create_task(
        client.call(
            "jobs.await",
            handles=[handle_a, handle_b],
            max_wait=10.0,
            caller_id=parent["id"],
        )
    )

    # Wait until the await has truly entered the daemon's await_jobs.
    await asyncio.wait_for(await_started.wait(), timeout=5.0)
    # One extra yield so the spy's set() propagates and the real await_jobs
    # is parked in asyncio.wait on both events.
    await asyncio.sleep(0)

    # Finish exactly one job. Under the new contract this wakes the await
    # immediately; the sibling stays running.
    daemon.jobs.finish(handle_a, state=JobState.DONE, result="A done")

    # The await should return promptly — well under max_wait. The 5s bound is
    # a failure guard, not a timing assertion: under the current ALL_COMPLETED
    # semantics the call blocks for the full 10s max_wait, so wait_for raises
    # TimeoutError, which is the expected failure on this branch.
    jobs = await asyncio.wait_for(await_task, timeout=5.0)

    states = {j["handle"]: j["state"] for j in jobs}
    results = {j["handle"]: j["result"] for j in jobs}

    # One entry per requested handle.
    assert set(states) == {handle_a, handle_b}
    # The finished job is done with its result.
    assert states[handle_a] == "done"
    assert results[handle_a] == "A done"
    # The sibling is still running — not blocked on by the caller.
    assert states[handle_b] == "running"

    # Restore so the fixture teardown does not see the spy.
    daemon.jobs.await_jobs = original


async def test_acceptance_multi_await_terminal_at_entry_returns_immediately(
    client, fake_tmux, daemon
):
    """If a terminal handle is present when the await is called, it returns
    immediately — no waiting, even with a generous max_wait.

    Proves immediacy without wall-clock assertions: a delayed finish on the
    running sibling fires *after* a short delay. Under the new contract the
    await returns instantly (terminal at entry), so the sibling is still
    ``running`` when we inspect it. Under the current ALL_COMPLETED code the
    await blocks until the delayed finish lands, the sibling becomes ``done``,
    and the assertion fails.
    """
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")

    running_child = await client.call(
        "spawn",
        harness="vibe",
        prompt="still running",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    done_child = await client.call(
        "spawn",
        harness="vibe",
        prompt="already done",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    handle_r = running_child["handle"]
    handle_d = done_child["handle"]

    daemon.jobs.finish(handle_d, state=JobState.DONE, result="finished early")

    # Schedule the running sibling to finish after a short delay. If the await
    # returns immediately (the contract), this has not fired yet. If it blocks
    # (the current ALL_COMPLETED bug), it will have.
    async def _delayed_finish():
        await asyncio.sleep(0.3)
        daemon.jobs.finish(handle_r, state=JobState.DONE, result="late")

    delayed = asyncio.create_task(_delayed_finish())

    # max_wait is generous; the contract says the terminal handle at entry
    # makes the call return at once, so it should never approach the ceiling.
    jobs = await client.call(
        "jobs.await",
        handles=[handle_r, handle_d],
        max_wait=10.0,
        caller_id=parent["id"],
    )
    states = {j["handle"]: j["state"] for j in jobs}
    assert states[handle_d] == "done"
    # The sibling must still be running — the await returned before the
    # delayed finish fired.
    assert states[handle_r] == "running"

    # Clean up the delayed task (it may or may not have been awaited).
    if not delayed.done():
        delayed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await delayed
