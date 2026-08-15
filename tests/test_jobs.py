"""Tests for the job state machine: spawn → await → result.

The full loop is exercised over the socket against a real daemon, with
tmux stubbed out (same FakeTmux as test_daemon.py). The observer is real,
but no harnesses are configured — the tests finish jobs directly via the
reaper or by calling the job manager, rather than by tailing transcripts.
"""

from __future__ import annotations

import pytest

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
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp", parent_id=parent["id"]
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


# ---- unknown handle -----------------------------------------------------


async def test_jobs_status_unknown_handle(client, fake_tmux):
    from theater.protocol import RemoteError

    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.status", handle="ghost")
    assert exc.value.code == "bad_request"


async def test_await_unknown_handle_is_an_error(client, fake_tmux):
    """It used to return [], which reads as "nothing to report".

    An agent cannot tell that apart from a job that has not finished, so it
    re-awaits the same dead handle until it gives up.
    """
    from theater.protocol import RemoteError

    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.await", handles=["ghost"], max_wait=0.1)
    assert exc.value.code == "bad_request"
    assert "ghost" in str(exc.value)


async def test_await_names_every_handle_it_could_not_find(client, fake_tmux):
    from theater.protocol import RemoteError

    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.await", handles=[record["handle"], "ghost"], max_wait=0.1)
    assert "ghost" in str(exc.value)


async def test_await_between_two_peers_blocked_on_each_other_is_refused(client, fake_tmux, daemon):
    """Two siblings, no ancestry between them: only the live graph sees this.

    Both would sit inside an MCP tool call unable to answer the other, and
    find out minutes later when their timeouts expire.
    """
    from theater.protocol import RemoteError

    a = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    b = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    job = await client.call("send", target=b["id"], prompt="a asks b", caller_id=a["id"])
    # B is already blocked on A, as if mid-`await_sessions`.
    with daemon.jobs.waiting(b["id"], [a["id"]]), pytest.raises(RemoteError) as exc:
        await client.call(
            "jobs.await",
            handles=[job["handle"]],
            max_wait=0.1,
            caller_id=a["id"],
        )
    assert exc.value.code == "cycle_detected"


async def test_the_wait_graph_empties_when_an_await_returns(client, fake_tmux, daemon):
    """An edge is a call in flight. A timeout ends the call, so it ends too."""
    a = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    b = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    job = await client.call("send", target=b["id"], prompt="a asks b", caller_id=a["id"])
    await client.call("jobs.await", handles=[job["handle"]], max_wait=0.05, caller_id=a["id"])
    assert daemon.jobs.wait_graph == {}


async def test_await_will_not_block_for_longer_than_the_ceiling(
    client, fake_tmux, daemon, monkeypatch
):
    """An agent asking for an hour gets five minutes, not an hour."""
    import theater.daemon.methods as methods_mod

    seen: list[float] = []
    real = daemon.jobs.await_jobs

    async def spy(handles, max_wait):
        seen.append(max_wait)
        return await real(handles, max_wait=0.01)

    monkeypatch.setattr(daemon.jobs, "await_jobs", spy)
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("jobs.await", handles=[record["handle"]], max_wait=3600)
    assert seen == [methods_mod.MAX_AWAIT]


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
