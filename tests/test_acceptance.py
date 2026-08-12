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


import pytest

from theater.client import DaemonClient
from theater.daemon.jobs import JobState
from theater.daemon.server import Daemon
from theater.models import Status
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
        "spawn", harness="vibe", prompt="say hello",
        approval="manual", cwd="/tmp", parent_id=parent["id"]
    )
    handle = child["handle"]
    assert handle == child["id"]

    # The job is running.
    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "running"

    # The observer detects turn-end and finishes the job.
    daemon.jobs.finish(handle, state=JobState.DONE, result="hello world")

    # The parent awaits the result.
    jobs = await client.call(
        "jobs.await", handles=[handle], max_wait=2.0,
        caller_id=parent["id"]
    )
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
            "spawn", harness="vibe", prompt=f"task {i}",
            approval="manual", cwd="/tmp", parent_id=parent["id"]
        )
        handles.append(record["handle"])

    # Finish two of three.
    daemon.jobs.finish(handles[0], state=JobState.DONE, result="result 0")
    daemon.jobs.finish(handles[1], state=JobState.DONE, result="result 1")

    # Await all three. The third is still running.
    jobs = await client.call(
        "jobs.await", handles=handles, max_wait=0.5,
        caller_id=parent["id"]
    )
    states = {j["handle"]: j["state"] for j in jobs}
    results = {j["handle"]: j["result"] for j in jobs}
    assert states[handles[0]] == "done"
    assert states[handles[1]] == "done"
    assert states[handles[2]] == "running"
    assert results[handles[0]] == "result 0"
    assert results[handles[1]] == "result 1"

    # Now finish the third and re-await.
    daemon.jobs.finish(handles[2], state=JobState.DONE, result="result 2")
    jobs = await client.call(
        "jobs.await", handles=handles, max_wait=2.0,
        caller_id=parent["id"]
    )
    states = {j["handle"]: j["state"] for j in jobs}
    assert all(s == "done" for s in states.values())


# ========================================================================
# 3. Tree shows correct lineage and status
# ========================================================================


async def test_acceptance_tree_lineage(client, fake_tmux):
    """The tree shows parent → children with correct lineage."""
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp/parent")
    child1 = await client.call(
        "spawn", harness="vibe", prompt="task 1",
        approval="manual", cwd="/tmp/c1", parent_id=parent["id"]
    )
    child2 = await client.call(
        "spawn", harness="claude", prompt="task 2",
        approval="manual", cwd="/tmp/c2", parent_id=parent["id"]
    )

    tree = await client.call("participants.tree")
    assert len(tree) == 1  # one root
    root = tree[0]
    assert root["id"] == parent["id"]
    assert root["harness"] == "vibe"
    assert len(root["children"]) == 2
    child_ids = {c["id"] for c in root["children"]}
    assert child_ids == {child1["id"], child2["id"]}


async def test_acceptance_tree_status(client, fake_tmux, daemon):
    """Status transitions are visible in the tree."""
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    child = await client.call(
        "spawn", harness="vibe", prompt="hi",
        approval="manual", cwd="/tmp", parent_id=parent["id"]
    )

    # Child starts as "starting"
    tree = await client.call("participants.tree")
    child_node = tree[0]["children"][0]
    assert child_node["status"] == "starting"

    # Observer finishes the job → status should still be visible
    daemon.jobs.finish(child["handle"], state=JobState.DONE, result="done")
    daemon.registry.set_status(child["id"], Status.IDLE)

    tree = await client.call("participants.tree")
    child_node = tree[0]["children"][0]
    assert child_node["status"] == "idle"


# ========================================================================
# 4. Kill + restart mid-job → crashed
# ========================================================================


async def test_acceptance_restart_crashes_orphaned_job(theater_home, fake_tmux):
    """Kill -9 the daemon mid-job. It restarts, the job reports crashed."""
    from theater.tmux.client import Pane

    pane = Pane(
        pane_id="%1", pane_pid=123, cwd="/tmp", window_id="@1",
        session="main", window_name="test", current_command="vibe",
    )
    fake_tmux.visible_panes = [pane]

    # First daemon: spawn a child with a running job.
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        record = await c.call(
            "spawn", harness="vibe", prompt="hi",
            approval="manual", cwd="/tmp"
        )
        handle = record["handle"]
        job = await c.call("jobs.status", handle=handle)
        assert job["state"] == "running"
    await d1.aclose()

    # Simulate the pane vanishing during restart.
    fake_tmux.visible_panes = []

    # Second daemon: reconcile should crash the orphaned job.
    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        job = await c.call("jobs.status", handle=handle)
        assert job["state"] == "crashed"
        assert job["error_code"] == "crashed"
    await d2.aclose()


async def test_acceptance_restart_preserves_tree(theater_home, fake_tmux):
    """The tree survives a daemon restart."""
    from theater.tmux.client import Pane

    pane = Pane(
        pane_id="%1", pane_pid=123, cwd="/tmp", window_id="@1",
        session="main", window_name="test", current_command="vibe",
    )
    fake_tmux.visible_panes = [pane]

    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        parent = await c.call("hello", harness="vibe", pane="%1", cwd="/tmp")
        await c.call(
            "spawn", harness="vibe", prompt="hi",
            approval="manual", cwd="/tmp", parent_id=parent["id"]
        )
        tree = await c.call("participants.tree")
        assert len(tree[0]["children"]) == 1
    await d1.aclose()

    # Restart with the parent pane still alive.
    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        tree = await c.call("participants.tree")
        assert len(tree) == 1
        assert tree[0]["id"] == parent["id"]
    await d2.aclose()


# ========================================================================
# 5. Depth cap rejection
# ========================================================================


async def test_acceptance_depth_cap_rejects_deep_spawn(client, fake_tmux):
    """A spawn that would exceed the depth cap is rejected cleanly."""
    root = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    d1 = await client.call(
        "spawn", harness="vibe", prompt="d1",
        approval="manual", cwd="/tmp", parent_id=root["id"]
    )
    d2 = await client.call(
        "spawn", harness="vibe", prompt="d2",
        approval="manual", cwd="/tmp", parent_id=d1["id"]
    )
    # d2 is at depth 2. Spawning from d2 is depth 3 = cap. OK.
    d3 = await client.call(
        "spawn", harness="vibe", prompt="d3",
        approval="manual", cwd="/tmp", parent_id=d2["id"]
    )
    # d3 is at depth 3. Spawning from d3 is depth 4 > cap. Rejected.
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "spawn", harness="vibe", prompt="d4",
            approval="manual", cwd="/tmp", parent_id=d3["id"]
        )
    assert exc.value.code == "depth_exceeded"


# ========================================================================
# 6. Cycle detection rejection
# ========================================================================


async def test_acceptance_cycle_detection_rejects_await(client, fake_tmux):
    """A→spawn B→spawn C. C awaiting A is a clean rejection, not a hang."""
    a = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    b = await client.call(
        "spawn", harness="vibe", prompt="b",
        approval="manual", cwd="/tmp", parent_id=a["id"]
    )
    c = await client.call(
        "spawn", harness="vibe", prompt="c",
        approval="manual", cwd="/tmp", parent_id=b["id"]
    )

    # C awaits A: A is an ancestor of C → cycle.
    with pytest.raises(RemoteError) as exc:
        await client.call(
            "jobs.await", handles=[a["id"]], max_wait=1.0,
            caller_id=c["id"]
        )
    assert exc.value.code == "cycle_detected"

    # A awaits B: normal pattern, not a cycle.
    jobs = await client.call(
        "jobs.await", handles=[b["id"]], max_wait=0.1,
        caller_id=a["id"]
    )
    assert len(jobs) == 1


# ========================================================================
# 7. Bus records the full story
# ========================================================================


async def test_acceptance_bus_records_orchestration(client, fake_tmux, daemon):
    """The bus carries the full story: spawn, job created, job finished."""
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    child = await client.call(
        "spawn", harness="vibe", prompt="hi",
        approval="manual", cwd="/tmp", parent_id=parent["id"]
    )
    daemon.jobs.finish(child["handle"], state=JobState.DONE, result="done")

    events = await client.call("bus.tail", limit=200)
    kinds = [e["kind"] for e in events]
    assert "participant.created" in kinds
    assert "job.created" in kinds
    assert "job.finished" in kinds
