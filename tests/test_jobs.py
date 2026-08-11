"""Tests for the job state machine: spawn → await → result.

The full loop is exercised over the socket against a real daemon, with
tmux stubbed out (same FakeTmux as test_daemon.py). The observer is real,
but no harnesses are configured — the tests finish jobs directly via the
reaper or by calling the job manager, rather than by tailing transcripts.
"""

from __future__ import annotations

import asyncio

import pytest

from theater.client import DaemonClient
from theater.daemon.server import Daemon


@pytest.fixture
def fake_tmux(monkeypatch):
    """Reuse the daemon tests' tmux stub."""
    import tests.test_daemon as td

    fake = td.FakeTmux()
    import theater.daemon.spawner as spawner_mod
    monkeypatch.setattr(spawner_mod.tmux, "new_window", fake.new_window)
    monkeypatch.setattr(spawner_mod.tmux, "ensure_session", fake.ensure_session)
    monkeypatch.setattr(spawner_mod.tmux, "sessions", fake.sessions)
    monkeypatch.setattr(spawner_mod.tmux, "kill_pane", fake.kill_pane)
    monkeypatch.setattr(spawner_mod.tmux, "list_panes", fake.list_panes)
    monkeypatch.setattr(spawner_mod.tmux, "available", fake.available)
    monkeypatch.setattr(spawner_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
    import theater.daemon.server as server_mod
    monkeypatch.setattr(server_mod.tmux, "list_panes", fake.list_panes)
    monkeypatch.setattr(server_mod.tmux, "available", fake.available)
    return fake


@pytest.fixture
async def daemon(theater_home):
    d = Daemon(harnesses={})
    await d.start()
    yield d
    await d.aclose()


@pytest.fixture
async def client(daemon):
    c = DaemonClient(autostart=False)
    await c.connect()
    yield c
    await c.aclose()


# ---- spawn creates a job ------------------------------------------------


async def test_spawn_creates_a_running_job(client, fake_tmux):
    record = await client.call(
        "spawn", harness="vibe", prompt="say hello", approval="manual", cwd="/tmp"
    )
    handle = record["handle"]
    assert handle == record["id"]

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "running"
    assert job["kind"] == "spawn"
    assert job["prompt"] == "say hello"
    assert job["target_id"] == record["id"]
    assert job["caller_id"] == "cli"


async def test_spawn_with_parent_sets_caller(client, fake_tmux):
    parent = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    child = await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual",
        cwd="/tmp", parent_id=parent["id"]
    )
    job = await client.call("jobs.status", handle=child["handle"])
    assert job["caller_id"] == parent["id"]


# ---- await returns done when the job finishes ---------------------------


async def test_await_returns_done_after_finish(client, fake_tmux, daemon):
    record = await client.call(
        "spawn", harness="vibe", prompt="say hello", approval="manual", cwd="/tmp"
    )
    handle = record["handle"]

    # Finish the job directly via the job manager (simulates observer
    # detecting turn-end).
    from theater.daemon.jobs import JobState
    daemon.jobs.finish(handle, state=JobState.DONE, result="hello world")

    jobs = await client.call("jobs.await", handles=[handle], max_wait=1.0)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "done"
    assert jobs[0]["result"] == "hello world"


# ---- await returns running on timeout -----------------------------------


async def test_await_returns_running_on_timeout(client, fake_tmux):
    record = await client.call(
        "spawn", harness="vibe", prompt="say hello", approval="manual", cwd="/tmp"
    )
    handle = record["handle"]

    jobs = await client.call("jobs.await", handles=[handle], max_wait=0.1)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "running"


# ---- await with multiple handles (fan-out) ------------------------------


async def test_await_fan_out(client, fake_tmux, daemon):
    from theater.daemon.jobs import JobState

    handles = []
    for i in range(3):
        record = await client.call(
            "spawn", harness="vibe", prompt=f"task {i}", approval="manual", cwd="/tmp"
        )
        handles.append(record["handle"])

    # Finish two of three
    daemon.jobs.finish(handles[0], state=JobState.DONE, result="result 0")
    daemon.jobs.finish(handles[1], state=JobState.DONE, result="result 1")

    jobs = await client.call("jobs.await", handles=handles, max_wait=0.5)
    states = {j["handle"]: j["state"] for j in jobs}
    results = {j["handle"]: j["result"] for j in jobs}
    assert states[handles[0]] == "done"
    assert states[handles[1]] == "done"
    assert states[handles[2]] == "running"
    assert results[handles[0]] == "result 0"
    assert results[handles[1]] == "result 1"


# ---- reaper crashes running jobs ----------------------------------------


async def test_reaper_crashes_running_jobs(client, fake_tmux, daemon, monkeypatch):
    import theater.daemon.server as server_mod

    record = await client.call(
        "spawn", harness="vibe", prompt="say hello", approval="manual", cwd="/tmp"
    )
    handle = record["handle"]

    # Simulate the pane vanishing
    monkeypatch.setattr(server_mod.tmux, "available", lambda: True)
    monkeypatch.setattr(server_mod.tmux, "run", _fake_list_panes(""))
    await daemon._reap_once()

    job = await client.call("jobs.status", handle=handle)
    assert job["state"] == "crashed"
    assert job["error_code"] == "crashed"


# ---- await on a crashed job returns crashed -----------------------------


async def test_await_returns_crashed(client, fake_tmux, daemon):
    from theater.daemon.jobs import JobState

    record = await client.call(
        "spawn", harness="vibe", prompt="say hello", approval="manual", cwd="/tmp"
    )
    handle = record["handle"]

    daemon.jobs.finish(handle, state=JobState.CRASHED, error_code="crashed")

    jobs = await client.call("jobs.await", handles=[handle], max_wait=1.0)
    assert jobs[0]["state"] == "crashed"
    assert jobs[0]["error_code"] == "crashed"


# ---- unknown handle -----------------------------------------------------


async def test_jobs_status_unknown_handle(client, fake_tmux):
    from theater.protocol import RemoteError

    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.status", handle="ghost")
    assert exc.value.code == "bad_request"


async def test_await_unknown_handle_is_ignored(client, fake_tmux):
    jobs = await client.call("jobs.await", handles=["ghost"], max_wait=0.1)
    assert jobs == []


# ---- bus events ---------------------------------------------------------


async def test_job_created_and_finished_on_bus(client, fake_tmux, daemon):
    from theater.daemon.jobs import JobState

    record = await client.call(
        "spawn", harness="vibe", prompt="say hello", approval="manual", cwd="/tmp"
    )
    handle = record["handle"]

    daemon.jobs.finish(handle, state=JobState.DONE, result="done")

    events = await client.call("bus.tail", limit=100)
    kinds = [e["kind"] for e in events]
    assert "job.created" in kinds
    assert "job.finished" in kinds


def _fake_list_panes(output: str):
    async def run(*args, **kwargs):
        return output
    return run
