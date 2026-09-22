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
from functools import partial
from pathlib import Path

import pytest

from theater.client import DaemonClient
from theater.constants.observation import OBSERVATION_FAILURE_GRACE
from theater.daemon.observation.service import Observer
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


@pytest.mark.parametrize(
    ("elapsed", "expected_state", "expected_error"),
    [(0.0, "running", None), (OBSERVATION_FAILURE_GRACE, "crashed", "transcript_identity_lost")],
)
async def test_restart_identity_loss_replay_respects_grace(
    theater_home, terminal_provider, monkeypatch, elapsed, expected_state, expected_error
):
    """Restart replay retains the persisted grace window without restarting its clock."""
    d1 = Daemon(harnesses={})
    try:
        await d1.start()
        async with DaemonClient(autostart=False) as c:
            record = await c.call(
                "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp"
            )
            handle = record["handle"]
            d1.observer.mark_transcript_identity_lost(record["id"], "rotation evidence")
            assert d1.observer.transcript_identity_lost(record["id"])
            job = await c.call("jobs.status", handle=handle)
            failed_at = d1.store.observation_error_timestamp(
                record["id"], "transcript_identity_lost"
            )
            assert failed_at is not None
            replay_now = max(job["created_at"], failed_at) + elapsed
    finally:
        await d1.aclose()

    monkeypatch.setattr(
        "theater.daemon.server.Observer", partial(Observer, wall_clock=lambda: replay_now)
    )
    d2 = Daemon(harnesses={})
    try:
        await d2.start()
        # No harness watcher is loaded; explicitly exercise its replay path.
        d2.observer._restore_transcript_identity_loss(record["id"])
        async with DaemonClient(autostart=False) as c:
            job = await c.call("jobs.status", handle=handle)
            assert job["state"] == expected_state
            assert job["error_code"] == expected_error
    finally:
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
