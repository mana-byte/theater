"""Job lifecycle over the socket: spawn, await, bus events and worker admission."""

from __future__ import annotations

import asyncio
import threading

import pytest

from tests.support.paths import fixture
from tests.support.presence import FakePresence
from theater.daemon import workers


@pytest.fixture(autouse=True)
def _absent_presence(request):
    """No human at any pane: these tests exercise job state, not presence."""
    if "daemon" not in request.fixturenames:
        yield
        return
    daemon = request.getfixturevalue("daemon")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            daemon,
            "presence",
            FakePresence(capture_screen=daemon.presence.capture_screen),
            raising=False,
        )
        yield


def _trust(daemon, participant_id: str) -> None:
    participant = daemon.registry.get(participant_id)
    participant.session_id = f"session-{participant_id}"
    participant.session_correlation = "operator"
    daemon.store.upsert_participant(participant)


async def test_spawn_creates_a_running_job(client, terminal_provider):
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


async def test_spawn_with_parent_sets_caller(client, terminal_provider):
    parent = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
    child = await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp", parent_id=parent["id"]
    )
    job = await client.call("jobs.status", handle=child["handle"])
    assert job["caller_id"] == parent["id"]


async def test_await_returns_done_after_finish(client, terminal_provider, daemon):
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


async def test_await_reports_what_a_finished_job_changed(
    client, terminal_provider, daemon, tmp_path
):
    """Modified vs read comes from the touch hashes; no paths means no evidence, not no changes."""
    from theater.daemon.jobs import JobState
    from theater.harness.base import EventPath

    (tmp_path / "edited.py").write_text("before")
    (tmp_path / "read.py").write_text("same")
    edited = await client.call(
        "spawn", harness="vibe", prompt="edit", approval="manual", cwd=str(tmp_path)
    )
    silent = await client.call(
        "spawn", harness="vibe", prompt="shell only", approval="manual", cwd=str(tmp_path)
    )
    daemon.jobs.replace_touch_accumulator(edited["handle"], cwd=str(tmp_path))
    daemon.jobs.observe_paths(
        edited["handle"], (EventPath("edited.py", "write"), EventPath("read.py", "read"))
    )
    (tmp_path / "edited.py").write_text("after")
    daemon.jobs.finish(edited["handle"], state=JobState.DONE, result="ok")
    daemon.jobs.finish(silent["handle"], state=JobState.DONE, result="ok")

    jobs = await client.call(
        "jobs.await", handles=[edited["handle"], silent["handle"]], max_wait=1.0
    )
    assert jobs[0]["changes"] == {
        "evidence": "tool_paths",
        "modified": ["edited.py"],
        "modified_count": 1,
        "read_count": 1,
    }
    assert jobs[1]["changes"] == {"evidence": "none"}


async def test_await_returns_running_on_timeout(client, terminal_provider):
    record = await client.call(
        "spawn", harness="vibe", prompt="say hello", approval="manual", cwd="/tmp"
    )
    handle = record["handle"]

    jobs = await client.call("jobs.await", handles=[handle], max_wait=0.1)
    assert len(jobs) == 1
    assert jobs[0]["state"] == "running"


async def test_await_fan_out(client, terminal_provider, daemon):
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


async def test_jobs_status_unknown_handle(client, terminal_provider):
    from theater.protocol import RemoteError

    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.status", handle="ghost")
    assert exc.value.code == "bad_request"


async def test_await_unknown_handle_is_an_error(client, terminal_provider):
    """It used to return [], which reads as "nothing to report".

    An agent cannot tell that apart from a job that has not finished, so it
    re-awaits the same dead handle until it gives up.
    """
    from theater.protocol import RemoteError

    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.await", handles=["ghost"], max_wait=0.1)
    assert exc.value.code == "bad_request"
    assert "ghost" in str(exc.value)


async def test_await_names_every_handle_it_could_not_find(client, terminal_provider):
    from theater.protocol import RemoteError

    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.await", handles=[record["handle"], "ghost"], max_wait=0.1)
    assert "ghost" in str(exc.value)


def _ready_screen(daemon, terminal_provider, participant_id: str) -> None:
    """Provider sends require a proven ready composer on screen."""
    binding = daemon.store.terminal_bindings.get(participant_id)
    screen = fixture("screens", "vibe_idle.txt")
    terminal_provider.screens[binding.terminal_id] = screen.read_text()


async def test_await_between_two_peers_blocked_on_each_other_is_refused(
    client, terminal_provider, daemon
):
    """Two siblings, no ancestry between them: only the live graph sees this.

    Both would sit inside an MCP tool call unable to answer the other, and
    find out minutes later when their timeouts expire.
    """
    from theater.protocol import RemoteError

    a = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
    b = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
    _trust(daemon, a["id"])
    _trust(daemon, b["id"])
    terminal_provider.bind(daemon, b["id"])
    _ready_screen(daemon, terminal_provider, b["id"])
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


async def test_the_wait_graph_empties_when_an_await_returns(client, terminal_provider, daemon):
    """An edge is a call in flight. A timeout ends the call, so it ends too."""
    a = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
    b = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
    _trust(daemon, a["id"])
    _trust(daemon, b["id"])
    terminal_provider.bind(daemon, b["id"])
    _ready_screen(daemon, terminal_provider, b["id"])
    job = await client.call("send", target=b["id"], prompt="a asks b", caller_id=a["id"])
    await client.call("jobs.await", handles=[job["handle"]], max_wait=0.05, caller_id=a["id"])
    assert daemon.jobs.wait_graph == {}


async def test_await_will_not_block_for_longer_than_the_ceiling(
    client, terminal_provider, daemon, monkeypatch
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


async def test_job_created_and_finished_on_bus(client, terminal_provider, daemon):
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
            timeout=10.0,
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


async def test_await_says_what_an_unknown_handle_most_likely_meant(
    client, terminal_provider, daemon
):
    """Agents guessed handles from names and bare sequence numbers, then retried blind."""
    from theater.daemon.jobs import JobState
    from theater.protocol import RemoteError

    record = await client.call(
        "spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp", name="scout"
    )
    daemon.jobs.finish(record["handle"], state=JobState.DONE, result="ok")
    send = f"{record['id']}#7"
    daemon.jobs.create(handle=send, caller_id="cli", target_id=record["id"], kind="send")
    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.await", handles=["scout", "#7", "7", "ghost"], max_wait=0.1)
    message = str(exc.value)
    assert f"'scout' is participant {record['id']}; await its job {send}" in message
    assert f"'#7' (did you mean {send}?)" in message
    assert f"'7' (did you mean {send}?)" in message
    assert "'ghost'" in message
    assert "cannot be predicted before that call returns" in message


async def test_worker_admission_is_bounded_and_cancellation_does_not_release_running_work(
    monkeypatch, caplog
):
    await workers.shutdown()
    monkeypatch.setattr(workers, "MAX_WORKERS", 1)
    caplog.set_level("DEBUG", logger="theater.timing")
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    ran: list[str] = []

    def blocking():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)

    first = asyncio.create_task(workers.to_thread(blocking, label="blocked"))
    try:
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        queued = asyncio.create_task(workers.to_thread(ran.append, "cancelled", label="queued"))
        await asyncio.sleep(0)
        assert not queued.done()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        remaining = asyncio.create_task(workers.to_thread(ran.append, "done", label="remaining"))
        await asyncio.sleep(0)
        assert not remaining.done()
        release.set()
        await remaining
    finally:
        release.set()
        await workers.shutdown()
    assert ran == ["done"]
    operations = {getattr(record, "theater.operation", None) for record in caplog.records}
    assert {"WORKER_WAIT", "WORKER_EXECUTION"} <= operations
