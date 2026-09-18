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
from theater.daemon.server import Daemon


def _repo(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path


async def test_restart_does_not_infer_death_from_a_missing_provider_terminal(
    theater_home, terminal_provider
):
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        record = await c.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
        binding = d1.store.terminal_bindings.get(record["id"])
        assert binding is not None
    await d1.aclose()

    terminal_provider.remove_terminal(binding.terminal_id)

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        rows = await c.call("participants.list", include_dead=True)
        assert len(rows) == 1
        assert rows[0]["status"] != "dead"
    await d2.aclose()


async def test_restart_retains_jobs_when_provider_exit_is_not_authoritative(
    theater_home, terminal_provider
):

    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        record = await c.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
        handle = record["handle"]
        job = await c.call("jobs.status", handle=handle)
        assert job["state"] == "running"
        binding = d1.store.terminal_bindings.get(record["id"])
        assert binding is not None
    await d1.aclose()

    terminal_provider.remove_terminal(binding.terminal_id)

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        job = await c.call("jobs.status", handle=handle)
        assert job["state"] == "running"
        assert job["error_code"] is None
    await d2.aclose()


async def test_restart_preserves_bus_history(theater_home, terminal_provider):
    """Bus events survive restart because they are in SQLite."""
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        await c.call("hello", harness="vibe", pane=None, cwd="/tmp")
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


async def test_restart_preserves_response_format_jobs(theater_home, terminal_provider):
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


async def test_restart_preserves_scratchpad(theater_home, terminal_provider, tmp_path):
    repo = _repo(tmp_path, "repo")
    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        caller = await c.call("hello", id="root", harness="vibe", cwd=str(repo))
        wrote = await c.call(
            "scratchpad.write",
            caller_id=caller["id"],
            namespace="handoff",
            value="survives",
        )
    await d1.aclose()

    d2 = Daemon(harnesses={})
    await d2.start()
    async with DaemonClient(autostart=False) as c:
        got = await c.call(
            "scratchpad.get",
            caller_id=caller["id"],
            namespace="handoff",
        )
    await d2.aclose()

    assert got == {
        "namespace": "handoff",
        "entries": {wrote["key"]: "survives"},
        "keys": [wrote["key"]],
        "truncated": False,
        "after_key": None,
    }


async def test_restart_identity_loss_replay_does_not_crash_fresh_job(
    theater_home, terminal_provider
):
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


async def test_restart_identity_loss_replay_crashes_old_job(theater_home, terminal_provider):
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


async def test_restart_preserves_resume_floor(theater_home, terminal_provider):
    """A persisted resume floor survives daemon restart."""
    from theater.resume_floor import UNKNOWN_FLOOR, floor_is_present

    d1 = Daemon(harnesses={})
    await d1.start()
    async with DaemonClient(autostart=False) as c:
        await c.call("hello", harness="vibe", pane=None, cwd="/tmp")
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
