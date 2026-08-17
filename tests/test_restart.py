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


async def test_restart_identity_loss_replay_does_not_crash_fresh_job(theater_home, fake_tmux):
    """A job created just before the daemon died survives restart identity-loss replay.

    The OBSERVATION_FAILURE_GRACE that protects other source errors also
    protects identity-loss job destruction during restart replay: quarantine
    begins immediately (the participant is marked ``transcript_identity_lost``)
    but the job is not crashed until the grace window elapses.
    """
    from theater.daemon import observer as observer_mod

    original_grace = observer_mod.OBSERVATION_FAILURE_GRACE

    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        record = await c.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
        handle = record["handle"]
        # Mark identity loss while the daemon is running.
        d1.observer.mark_transcript_identity_lost(record["id"], "rotation evidence")
        assert d1.observer.transcript_identity_lost(record["id"])
    await d1.aclose()

    # Set grace high so the freshly-restarted replay does not crash the job.
    observer_mod.OBSERVATION_FAILURE_GRACE = 30.0
    try:
        d2 = Daemon(harnesses={})
        await d2.start()
        # Directly call the replay — the harness is not loaded so the observer
        # loop will not reach it, but _restore_transcript_identity_loss is the
        # code path under test.
        d2.observer.jobs = d2.jobs
        d2.observer._restore_transcript_identity_loss(record["id"])
        async with DaemonClient(autostart=False) as c:
            job = await c.call("jobs.status", handle=handle)
            assert job["state"] == "running"
    finally:
        observer_mod.OBSERVATION_FAILURE_GRACE = original_grace
    await d2.aclose()


async def test_restart_identity_loss_replay_crashes_old_job(theater_home, fake_tmux):
    """A job that predates the grace window is crashed by restart replay."""
    from theater.daemon import observer as observer_mod

    original_grace = observer_mod.OBSERVATION_FAILURE_GRACE

    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        record = await c.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
        handle = record["handle"]
        d1.observer.mark_transcript_identity_lost(record["id"], "rotation evidence")
    await d1.aclose()

    # Zero grace: the replay should crash the job immediately.
    observer_mod.OBSERVATION_FAILURE_GRACE = 0.0
    try:
        d2 = Daemon(harnesses={})
        await d2.start()
        d2.observer.jobs = d2.jobs
        d2.observer._restore_transcript_identity_loss(record["id"])
        async with DaemonClient(autostart=False) as c:
            job = await c.call("jobs.status", handle=handle)
            assert job["state"] == "crashed"
            assert job["error_code"] == "transcript_identity_lost"
    finally:
        observer_mod.OBSERVATION_FAILURE_GRACE = original_grace
    await d2.aclose()


async def test_restart_preserves_resume_floor(theater_home, fake_tmux):
    """A persisted resume floor survives daemon restart."""
    from theater.resume_floor import UNKNOWN_FLOOR, floor_is_present

    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        await c.call("hello", harness="vibe", pane="%1", cwd="/tmp")
        rows = await c.call("participants.list")
        pid = rows[0]["id"]
        p = d1.registry.store.get_participant(pid)
        p.resume_floor = UNKNOWN_FLOOR
        d1.registry.store.upsert_participant(p)
    await d1.aclose()

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        rows = await c.call("participants.list", include_dead=True)
        p = d2.registry.store.get_participant(rows[0]["id"])
        assert floor_is_present(p.resume_floor)
        assert p.resume_floor == UNKNOWN_FLOOR
    await d2.aclose()
