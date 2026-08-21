"""Tests for the job state machine: spawn → await → result.

The full loop is exercised over the socket against a real daemon, with
tmux stubbed out (same FakeTmux as test_daemon.py). The observer is real,
but no harnesses are configured — the tests finish jobs directly via the
reaper or by calling the job manager, rather than by tailing transcripts.
"""

from __future__ import annotations

import asyncio
import json

import pytest


def _trust(daemon, participant_id: str) -> None:
    participant = daemon.registry.get(participant_id)
    participant.session_id = f"session-{participant_id}"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)


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
    monkeypatch.setattr(server_mod.tmux, "run", _fake_list_panes("%other"))
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
    _trust(daemon, a["id"])
    _trust(daemon, b["id"])
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
    _trust(daemon, a["id"])
    _trust(daemon, b["id"])
    job = await client.call("send", target=b["id"], prompt="a asks b", caller_id=a["id"])
    await client.call("jobs.await", handles=[job["handle"]], max_wait=0.05, caller_id=a["id"])
    assert daemon.jobs.wait_graph == {}


async def test_await_will_not_block_for_longer_than_the_ceiling(
    client, fake_tmux, daemon, monkeypatch
):
    """An agent asking for an hour gets five minutes, not an hour."""
    import theater.daemon.rpc.jobs as jobs_mod

    seen: list[float] = []
    real = daemon.jobs.await_jobs

    async def spy(handles, max_wait):
        seen.append(max_wait)
        return await real(handles, max_wait=0.01)

    monkeypatch.setattr(daemon.jobs, "await_jobs", spy)
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("jobs.await", handles=[record["handle"]], max_wait=3600)
    assert seen == [jobs_mod.MAX_AWAIT]


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


# ---- await returns when ANY job finishes (first-completed semantic) -------


async def test_await_returns_when_first_of_two_finishes(daemon, monkeypatch):
    """Two initially-running jobs: finishing one mid-await returns promptly.

    The other job must remain ``running`` in the returned list. Event
    coordination drives the finish — the short ``wait_for`` is only a
    deadlock bound.
    """
    from theater.daemon.jobs import JobState

    jm = daemon.jobs

    jm.create(handle="h1", caller_id="cli", target_id="t1", kind="spawn")
    jm.create(handle="h2", caller_id="cli", target_id="t2", kind="spawn")

    # Patch asyncio.wait so the finisher runs only once await_jobs is
    # actually blocked inside the real wait.
    real_wait = asyncio.wait
    started = asyncio.Event()

    async def gate_wait(fs, **kw):
        started.set()
        return await real_wait(fs, **kw)

    monkeypatch.setattr(asyncio, "wait", gate_wait)

    async def finish_after_signal():
        await started.wait()
        jm.finish("h1", state=JobState.DONE, result="done h1")

    finisher = asyncio.create_task(finish_after_signal())
    try:
        jobs = await asyncio.wait_for(
            jm.await_jobs(["h1", "h2"], max_wait=5.0),
            timeout=3.0,
        )
    finally:
        if not finisher.done():
            finisher.cancel()
            await asyncio.gather(finisher, return_exceptions=True)

    by_handle = {j.handle: j for j in jobs}
    assert len(jobs) == 2
    assert by_handle["h1"].state == "done"
    assert by_handle["h2"].state == "running"


async def test_await_returns_immediately_when_one_already_terminal(daemon, monkeypatch):
    """One terminal + one running at entry: return without waiting at all.

    ``asyncio.wait`` must not be called; the test fails if it is.
    """
    from theater.daemon.jobs import JobState

    jm = daemon.jobs

    jm.create(handle="h1", caller_id="cli", target_id="t1", kind="spawn")
    jm.create(handle="h2", caller_id="cli", target_id="t2", kind="spawn")

    jm.finish("h1", state=JobState.DONE, result="already done")

    called = False

    def fail_wait(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(asyncio, "wait", fail_wait)

    jobs = await jm.await_jobs(["h1", "h2"], max_wait=5.0)

    by_handle = {j.handle: j for j in jobs}
    assert len(jobs) == 2
    assert by_handle["h1"].state == "done"
    assert by_handle["h2"].state == "running"
    assert not called


# ---- structured JSON transport ------------------------------------------


def test_structured_json_keeps_full_raw_result_and_clipped_legacy_result(store):
    from theater.daemon.jobs import JobManager, JobState
    from theater.harness.base import MAX_TEXT, clip

    raw = json.dumps({"answer": "x" * (MAX_TEXT + 500)})
    assert len(raw) > MAX_TEXT

    jm = JobManager(store)
    jm.create(
        handle="json-long",
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format='{"type":"json_schema"}',
    )
    jm.finish(
        "json-long",
        state=JobState.DONE,
        result=clip(raw),
        raw_result=raw,
    )

    job = jm.get("json-long")
    assert job is not None
    assert job.result == clip(raw)
    assert job.structured_result == raw
    assert json.loads(job.structured_result) == {"answer": "x" * (MAX_TEXT + 500)}
    assert job.structured_status == "parsed"


@pytest.mark.parametrize(
    "raw",
    [
        "{not json}",
        '```json\n{"answer": 42}\n```',
        '{"answer": 42}\ntrailing prose',
    ],
)
def test_structured_json_invalid_fenced_or_trailing_prose_is_unavailable(store, raw):
    from theater.daemon.jobs import JobManager, JobState

    jm = JobManager(store)
    handle = f"bad-{len(raw)}"
    jm.create(
        handle=handle,
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format="json",
    )
    jm.finish(handle, state=JobState.DONE, result=raw[:20], raw_result=raw)

    job = jm.get(handle)
    assert job is not None
    assert job.structured_result is None
    assert job.structured_status == "unavailable"


def test_structured_json_recursion_error_is_unavailable(store, monkeypatch):
    import theater.daemon.jobs as jobs_mod
    from theater.daemon.jobs import JobManager, JobState

    def too_deep(_raw):
        raise RecursionError("too deeply nested")

    monkeypatch.setattr(jobs_mod.json, "loads", too_deep)
    jm = JobManager(store)
    jm.create(
        handle="bad-recursion",
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format="json",
    )

    jm.finish(
        "bad-recursion",
        state=JobState.DONE,
        result="[[...]]",
        raw_result="[[...]]",
    )

    job = jm.get("bad-recursion")
    assert job is not None
    assert job.structured_result is None
    assert job.structured_status == "unavailable"


def test_structured_json_null_is_a_parsed_raw_result(store):
    from theater.daemon.jobs import JobManager, JobState

    jm = JobManager(store)
    jm.create(
        handle="json-null",
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format="json",
    )
    jm.finish("json-null", state=JobState.DONE, result="null", raw_result="null")

    job = jm.get("json-null")
    assert job is not None
    assert job.structured_result == "null"
    assert json.loads(job.structured_result) is None
    assert job.structured_status == "parsed"


@pytest.mark.parametrize(
    "state,error_code",
    [
        ("killed", "killed"),
        ("crashed", "crashed"),
        ("crashed", "send_failed"),
    ],
)
def test_structured_json_terminal_failures_are_unavailable(store, state, error_code):
    from theater.daemon.jobs import JobManager

    jm = JobManager(store)
    jm.create(
        handle=f"{state}-{error_code}",
        caller_id="cli",
        target_id="target",
        kind="send",
        response_format="json",
    )
    jm.finish(
        f"{state}-{error_code}",
        state=state,
        result='{"answer": 42}',
        error_code=error_code,
        raw_result='{"answer": 42}',
    )

    job = jm.get(f"{state}-{error_code}")
    assert job is not None
    assert job.structured_result is None
    assert job.structured_status == "unavailable"


def test_structured_json_and_touch_rows_commit_together(store, tmp_path):
    from theater.daemon.jobs import JobManager, JobState
    from theater.daemon.schema import touch
    from theater.harness.base import EventPath

    touched = tmp_path / "touched.py"
    touched.write_text("before")
    jobs = JobManager(store)
    jobs.create(
        handle="json-touch",
        caller_id="cli",
        target_id="target",
        kind="send",
        cwd=str(tmp_path),
        response_format="json",
    )
    jobs.observe_paths("json-touch", (EventPath(path="touched.py", mode="write"),))
    touched.write_text("after")

    jobs.finish(
        "json-touch",
        state=JobState.DONE,
        result='{"ok": true}',
        raw_result='{"ok": true}',
    )

    job = jobs.get("json-touch")
    assert job is not None
    assert job.structured_result == '{"ok": true}'
    assert job.structured_status == "parsed"
    rows = list(store.conn.execute(touch.select().where(touch.c.job_handle == "json-touch")))
    assert len(rows) == 1
