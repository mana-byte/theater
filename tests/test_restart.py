"""Tests for daemon restart and reconciliation.

The exit criteria: kill -9 the daemon mid-job. It restarts, the tree is
intact, the orphaned job reports crashed to its caller.

These tests simulate a restart by:
  1. Creating a daemon, spawning a participant with a running job
  2. Closing the daemon (simulating kill -9)
  3. Creating a new daemon with the same store (simulating restart)
  4. Checking that reconciliation marks dead participants and crashes jobs
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from theater.client import DaemonClient
from theater.daemon.jobs import JobState
from theater.daemon.server import Daemon


def _repo(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


async def test_restart_preserves_participants(theater_home, fake_tmux):
    """A restarted daemon sees the same participants from SQLite."""
    from theater.tmux.client import Pane

    pane = Pane(
        pane_id="%1",
        pane_pid=123,
        cwd="/tmp",
        window_id="@1",
        session="main",
        window_name="test",
        current_command="vibe",
    )
    fake_tmux.visible_panes = [pane]

    # First daemon: create a participant.
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        await c.call("hello", harness="vibe", pane="%1", cwd="/tmp")
        rows = await c.call("participants.list")
        assert len(rows) == 1
    await d1.aclose()

    # Second daemon: same store, should see the participant.
    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        rows = await c.call("participants.list")
        assert len(rows) == 1
        assert rows[0]["tmux_pane"] == "%1"
    await d2.aclose()


async def test_restart_marks_dead_participants_whose_panes_vanished(theater_home, fake_tmux):
    """A participant whose pane is gone after restart is marked dead."""
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        await c.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    await d1.aclose()

    # Simulate the pane vanishing: clear visible_panes before restart.
    fake_tmux.visible_panes = []

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        rows = await c.call("participants.list", include_dead=True)
        assert len(rows) == 1
        assert rows[0]["status"] == "dead"
    await d2.aclose()


async def test_restart_crashes_orphaned_jobs(theater_home, fake_tmux):
    """A running job whose target died during restart is marked crashed."""

    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        record = await c.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
        handle = record["handle"]
        job = await c.call("jobs.status", handle=handle)
        assert job["state"] == "running"
    await d1.aclose()

    # Simulate the pane vanishing during restart.
    fake_tmux.visible_panes = []

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        job = await c.call("jobs.status", handle=handle)
        assert job["state"] == "crashed"
        assert job["error_code"] == "crashed"
    await d2.aclose()


async def test_restart_preserves_bus_history(theater_home, fake_tmux):
    """Bus events survive restart because they are in SQLite."""
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        await c.call("hello", harness="vibe", pane="%1", cwd="/tmp")
        events1 = await c.call("bus.tail", limit=100)
    await d1.aclose()

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        events2 = await c.call("bus.tail", limit=100)
    await d2.aclose()

    # Same events (minus any reconcile-generated events)
    original_kinds = [e["kind"] for e in events1]
    restarted_kinds = [e["kind"] for e in events2]
    for kind in original_kinds:
        assert kind in restarted_kinds


async def test_restart_preserves_lineage(theater_home, fake_tmux):
    """The tree structure survives restart."""
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        parent = await c.call("hello", harness="vibe", pane="%1", cwd="/tmp")
        await c.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=parent["id"],
        )
        tree = await c.call("participants.tree")
        assert len(tree) == 1
        assert tree[0]["id"] == parent["id"]
        assert len(tree[0]["children"]) == 1
    await d1.aclose()

    # Restart with pane still alive.
    from theater.tmux.client import Pane

    fake_tmux.visible_panes = [
        Pane(
            pane_id="%1",
            pane_pid=123,
            cwd="/tmp",
            window_id="@1",
            session="main",
            window_name="test",
            current_command="vibe",
        )
    ]

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        tree = await c.call("participants.tree")
        assert len(tree) == 1
        assert tree[0]["id"] == parent["id"]
        # Child pane is gone (not in visible_panes), so it's dead and
        # excluded from the default tree. But the parent is still there.
    await d2.aclose()


async def test_restart_preserves_response_format_jobs(theater_home, fake_tmux):
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        record = await c.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            response_format={"type": "object"},
        )
        handle = record["handle"]
        before = await c.call("jobs.status", handle=handle)
    await d1.aclose()

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        after = await c.call("jobs.status", handle=handle)
    await d2.aclose()

    assert before["response_format"] == '{"type":"object"}'
    assert after["response_format"] == before["response_format"]
    assert after["prompt"] == before["prompt"]


async def test_restart_preserves_tree_store(theater_home, fake_tmux, tmp_path):
    repo = _repo(tmp_path, "repo")
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        caller = await c.call("hello", id="root", harness="vibe", cwd=str(repo))
        await c.call(
            "store.put",
            caller_id=caller["id"],
            namespace="handoff",
            key="summary",
            value="survives",
        )
    await d1.aclose()

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        got = await c.call(
            "store.get",
            caller_id=caller["id"],
            namespace="handoff",
            key="summary",
        )
    await d2.aclose()

    assert got == {"value": "survives"}


async def test_restart_preserves_checkpoints(theater_home, fake_tmux):
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        caller = await c.call("hello", id="caller", harness="vibe", cwd="/tmp")
        d1.jobs.create(
            handle="caller#1",
            caller_id=caller["id"],
            target_id=None,
            kind="send",
            prompt="remember this",
        )
        created = await c.call(
            "checkpoint.create",
            caller_id=caller["id"],
            name="before restart",
        )
        d1.jobs.finish("caller#1", state=JobState.DONE, result="done")
    await d1.aclose()

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        read = await c.call("checkpoint.read", checkpoint_id=created["checkpoint_id"])
    await d2.aclose()

    assert read["checkpoint"]["name"] == "before restart"
    assert [job["handle"] for job in read["recorded_jobs"]] == ["caller#1"]
    assert read["recorded_jobs"][0]["state"] == "running"
    assert read["live_jobs"][0]["state"] == "done"
