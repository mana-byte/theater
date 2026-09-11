"""Presence-aware await over real jobs, rails, bus rows, and daemon sockets."""

from __future__ import annotations

import asyncio
import time

import pytest
from presence_fakes import ABSENT, PRESENT, UNKNOWN, FakePresence

from theater.daemon import methods
from theater.daemon.jobs import JobState
from theater.daemon.rpc import METHODS

WAIT = 2.0


@pytest.fixture
def presence(daemon):
    fake = FakePresence()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", fake, raising=False)
        yield fake


async def _spawn(client, prompt="task"):
    return await client.call("spawn", harness="vibe", prompt=prompt, approval="manual", cwd="/tmp")


async def _await_rpc(daemon, params):
    """Call the raw handler so tests can cancel the daemon-side await."""
    return await METHODS["jobs.await"](daemon, params)


def _status(daemon, participant_id):
    participant = daemon.store.get_participant(participant_id)
    return str(participant.status) if participant else None


def _reasons(jobs):
    return {job["handle"]: job["await_reason"] for job in jobs}


# ---- done-but-held --------------------------------------------------------


async def test_a_done_job_is_held_while_a_human_is_present(client, daemon, presence):
    record = await _spawn(client)
    handle = record["handle"]
    daemon.jobs.finish(handle, state=JobState.DONE, result="done text")
    presence.set(record["id"], PRESENT)

    task = asyncio.create_task(client.call("jobs.await", handles=[handle], max_wait=WAIT))
    await asyncio.sleep(0.05)
    assert not task.done()

    presence.set(record["id"], ABSENT)
    jobs = await asyncio.wait_for(task, 1.0)
    assert jobs[0]["state"] == "done"
    assert jobs[0]["await_reason"] == "presence_released"
    assert jobs[0]["human_presence"] == {
        "state": "absent",
        "protected": False,
        "reason": "no focus",
        "revision": presence.revision,
        "observed_at": jobs[0]["human_presence"]["observed_at"],
    }
    assert jobs[0]["participant_status"] == _status(daemon, record["id"])


async def test_a_held_done_job_times_out_and_grants_nothing(client, daemon, presence, monkeypatch):
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    record = await _spawn(client)
    daemon.jobs.finish(record["handle"], state=JobState.DONE, result="done text")
    presence.set(record["id"], PRESENT)

    jobs = await client.call(
        "jobs.await", handles=[record["handle"]], caller_id="cli", max_wait=0.05
    )
    assert jobs[0]["state"] == "done"
    assert jobs[0]["await_reason"] == "timeout"
    assert jobs[0]["human_presence"]["protected"] is True
    ends = [e for e in await client.call("bus.tail") if e["kind"] == "job.await.end"]
    assert [e["payload"]["state"] for e in ends] == ["timeout"]


# ---- human entering during an await --------------------------------------


async def test_a_human_entering_during_an_await_holds_it(client, daemon, presence):
    record = await _spawn(client)
    handle = record["handle"]

    task = asyncio.create_task(client.call("jobs.await", handles=[handle], max_wait=WAIT))
    await asyncio.sleep(0.05)
    presence.set(record["id"], PRESENT)
    daemon.jobs.finish(handle, state=JobState.DONE, result="late finish")
    await asyncio.sleep(0.15)
    assert not task.done()

    presence.set(record["id"], ABSENT)
    jobs = await asyncio.wait_for(task, 1.0)
    assert jobs[0]["state"] == "done"
    assert jobs[0]["await_reason"] == "presence_released"


async def test_a_departing_human_releases_a_still_working_job(client, daemon, presence):
    record = await _spawn(client)
    presence.set(record["id"], PRESENT)

    task = asyncio.create_task(client.call("jobs.await", handles=[record["handle"]], max_wait=WAIT))
    await asyncio.sleep(0.05)
    assert not task.done()

    presence.set(record["id"], ABSENT)
    jobs = await asyncio.wait_for(task, 1.0)
    assert jobs[0]["state"] == "running"
    assert jobs[0]["await_reason"] == "presence_released"
    assert jobs[0]["participant_status"] == _status(daemon, record["id"])


async def test_unknown_presence_protects_like_a_present_human(client, daemon, presence):
    record = await _spawn(client)
    daemon.jobs.finish(record["handle"], state=JobState.DONE, result="done")
    presence.set(record["id"], UNKNOWN)

    jobs = await client.call("jobs.await", handles=[record["handle"]], max_wait=0.1)
    assert jobs[0]["await_reason"] == "timeout"
    assert jobs[0]["human_presence"]["state"] == "unknown"
    assert jobs[0]["human_presence"]["protected"] is True


async def test_a_missing_provider_never_grants_absence(client, daemon, presence):
    daemon.presence = None
    record = await _spawn(client)
    daemon.jobs.finish(record["handle"], state=JobState.DONE, result="done")

    jobs = await client.call("jobs.await", handles=[record["handle"]], max_wait=0.1)
    assert jobs[0]["state"] == "done"
    assert jobs[0]["await_reason"] == "timeout"
    assert jobs[0]["human_presence"]["state"] == "unknown"
    assert jobs[0]["human_presence"]["protected"] is True


# ---- no-job participant handles -------------------------------------------


async def test_a_participant_with_no_job_waits_only_for_presence(client, daemon, presence):
    row = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    presence.set(row["id"], PRESENT)

    task = asyncio.create_task(client.call("jobs.await", handles=[row["id"]], max_wait=WAIT))
    await asyncio.sleep(0.05)
    assert not task.done()

    presence.set(row["id"], ABSENT)
    entries = await asyncio.wait_for(task, 1.0)
    entry = entries[0]
    expected_keys = {"handle", "target_id", "human_presence", "participant_status", "await_reason"}
    assert set(entry) == expected_keys
    assert (entry["handle"], entry["target_id"]) == (row["id"], row["id"])
    assert entry["await_reason"] == "presence_released"
    assert entry["human_presence"]["state"] == "absent"
    assert entry["participant_status"] == _status(daemon, row["id"])


async def test_an_already_absent_participant_returns_its_current_status(client, daemon, presence):
    row = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")

    started = time.monotonic()
    entries = await client.call("jobs.await", handles=[row["id"]], max_wait=WAIT)
    assert time.monotonic() - started < 0.5
    assert entries[0]["await_reason"] == "already_absent"
    assert entries[0]["participant_status"] == _status(daemon, row["id"])
    assert entries[0]["human_presence"]["protected"] is False


# ---- wait-any across mixed targets ----------------------------------------


async def test_wait_any_marks_nonqualifying_peers_pending(client, daemon, presence):
    held = await _spawn(client, prompt="held")
    free = await _spawn(client, prompt="free")
    presence.set(held["id"], PRESENT)

    task = asyncio.create_task(
        client.call("jobs.await", handles=[held["handle"], free["handle"]], max_wait=WAIT)
    )
    await asyncio.sleep(0.05)
    daemon.jobs.finish(free["handle"], state=JobState.DONE, result="free done")

    jobs = await asyncio.wait_for(task, 1.0)
    assert [job["handle"] for job in jobs] == [held["handle"], free["handle"]]
    reasons = _reasons(jobs)
    assert reasons[free["handle"]] == "job_terminal"
    assert reasons[held["handle"]] == "pending"


async def test_an_already_absent_peer_releases_a_held_wait_any(client, daemon, presence):
    held = await _spawn(client, prompt="held")
    daemon.jobs.finish(held["handle"], state=JobState.DONE, result="done")
    presence.set(held["id"], PRESENT)
    row = await client.call("hello", harness="vibe", pane="%3", cwd="/tmp")

    jobs = await client.call("jobs.await", handles=[held["handle"], row["id"]], max_wait=WAIT)
    reasons = _reasons(jobs)
    assert reasons[row["id"]] == "already_absent"
    assert reasons[held["handle"]] == "pending"


async def test_one_terminal_and_one_absent_job_return_without_waiting(client, daemon, presence):
    first = await _spawn(client, prompt="first")
    second = await _spawn(client, prompt="second")
    daemon.jobs.finish(first["handle"], state=JobState.DONE, result="first done")

    jobs = await client.call(
        "jobs.await", handles=[first["handle"], second["handle"]], max_wait=WAIT
    )
    reasons = _reasons(jobs)
    assert reasons[first["handle"]] == "job_terminal"
    assert reasons[second["handle"]] == "pending"


# ---- deadline and max_wait=0 ---------------------------------------------


async def test_zero_max_wait_grants_nothing(client, daemon, presence):
    record = await _spawn(client)
    presence.set(record["id"], PRESENT)

    jobs = await client.call("jobs.await", handles=[record["handle"]], max_wait=0)
    assert jobs[0]["state"] == "running"
    assert jobs[0]["await_reason"] == "timeout"


async def test_zero_max_wait_still_returns_an_already_qualifying_target(client, daemon, presence):
    record = await _spawn(client)
    daemon.jobs.finish(record["handle"], state=JobState.DONE, result="done")

    jobs = await client.call("jobs.await", handles=[record["handle"]], max_wait=0)
    assert jobs[0]["await_reason"] == "job_terminal"


async def test_the_deadline_expires_with_timeout_only(client, daemon, presence):
    record = await _spawn(client)
    presence.set(record["id"], PRESENT)

    started = time.monotonic()
    jobs = await client.call("jobs.await", handles=[record["handle"]], max_wait=0.2)
    elapsed = time.monotonic() - started
    assert jobs[0]["await_reason"] == "timeout"
    assert elapsed >= 0.2


# ---- cycles ---------------------------------------------------------------


async def test_a_presence_only_await_cannot_close_a_live_wait_cycle(client, daemon, presence):
    from theater.protocol import RemoteError

    a = await client.call("hello", harness="vibe", pane="%1", cwd="/tmp")
    b = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    presence.set(b["id"], PRESENT)

    blocked = asyncio.create_task(
        _await_rpc(daemon, {"handles": [b["id"]], "caller_id": a["id"], "max_wait": 5.0})
    )
    await asyncio.sleep(0.05)
    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.await", handles=[a["id"]], caller_id=b["id"], max_wait=0.1)
    assert exc.value.code == "cycle_detected"
    blocked.cancel()
    await asyncio.gather(blocked, return_exceptions=True)


# ---- rapid transitions and the subscription race -------------------------


async def test_coalesced_transitions_do_not_release_a_protected_target(client, daemon, presence):
    record = await _spawn(client)

    task = asyncio.create_task(client.call("jobs.await", handles=[record["handle"]], max_wait=WAIT))
    await asyncio.sleep(0.05)
    for state in (PRESENT, ABSENT, PRESENT, ABSENT, PRESENT):
        presence.set(record["id"], state)
    await asyncio.sleep(0.1)
    assert not task.done()

    presence.set(record["id"], ABSENT)
    jobs = await asyncio.wait_for(task, 1.0)
    assert jobs[0]["state"] == "running"
    assert jobs[0]["await_reason"] == "presence_released"


async def test_a_departure_before_the_await_was_never_a_hold(client, daemon, presence):
    record = await _spawn(client)
    presence.set(record["id"], PRESENT)
    presence.set(record["id"], ABSENT)

    jobs = await client.call("jobs.await", handles=[record["handle"]], max_wait=0.15)
    assert jobs[0]["state"] == "running"
    assert jobs[0]["await_reason"] == "timeout"


# ---- cancellation cleanup -------------------------------------------------


async def test_a_cancelled_await_leaves_no_graph_waiters_or_open_bus_rows(
    client, daemon, presence, monkeypatch
):
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    caller = await client.call("hello", harness="vibe", pane="%3", cwd="/tmp")
    record = await _spawn(client)
    presence.set(record["id"], PRESENT)

    task = asyncio.create_task(
        _await_rpc(
            daemon,
            {"handles": [record["handle"]], "caller_id": caller["id"], "max_wait": 5.0},
        )
    )
    await asyncio.sleep(0.05)
    assert daemon.jobs.wait_graph != {}
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert daemon.jobs.wait_graph == {}
    assert presence._waiters == set()
    events = [e for e in await client.call("bus.tail") if e["kind"].startswith("job.await")]
    kinds = [e["kind"] for e in events]
    assert kinds.count("job.await.start") == kinds.count("job.await.end")
    end_states = [e["payload"]["state"] for e in events if e["kind"] == "job.await.end"]
    assert end_states == ["cancelled"]


async def test_a_held_await_announces_and_closes_its_bus_rows(
    client, daemon, presence, monkeypatch
):
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    record = await _spawn(client)
    presence.set(record["id"], PRESENT)

    task = asyncio.create_task(
        client.call("jobs.await", handles=[record["handle"]], caller_id="cli", max_wait=WAIT)
    )
    await asyncio.sleep(0.05)
    presence.set(record["id"], ABSENT)
    await asyncio.wait_for(task, 1.0)

    events = [e for e in await client.call("bus.tail") if e["kind"].startswith("job.await")]
    assert [e["kind"] for e in events] == ["job.await.start", "job.await.end"]
    assert events[1]["payload"]["state"] == "presence_released"


async def test_anonymous_await_does_not_publish_an_announcement(
    client, daemon, presence, monkeypatch
):
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    row = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    presence.set(row["id"], PRESENT)
    await client.call("jobs.await", handles=[row["id"]], max_wait=0.02)
    assert not [e for e in await client.call("bus.tail") if e["kind"].startswith("job.await")]


async def test_admission_refresh_is_inside_the_single_deadline(
    client, daemon, presence, monkeypatch
):
    row = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def blocked_refresh():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(presence, "refresh", blocked_refresh)
    entries = await asyncio.wait_for(
        client.call("jobs.await", handles=[row["id"]], max_wait=0.02), timeout=1
    )
    assert entered.is_set() and cancelled.is_set()
    assert entries[0]["await_reason"] == "timeout"
    assert entries[0]["human_presence"]["state"] == "unknown"


async def test_failed_refresh_cannot_release_cached_absence(client, daemon, presence, monkeypatch):
    row = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")

    async def failed_refresh():
        raise OSError("inventory unavailable")

    monkeypatch.setattr(presence, "refresh", failed_refresh)
    entries = await client.call("jobs.await", handles=[row["id"]], max_wait=0.02)
    assert entries[0]["await_reason"] == "timeout"
    assert entries[0]["human_presence"]["state"] == "unknown"


async def test_failed_refresh_can_release_after_a_successful_new_revision(
    client, daemon, presence, monkeypatch
):
    row = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
    subscribed = asyncio.Event()
    broken = True
    original_wait = presence.wait_for_change

    async def refresh():
        if broken:
            raise OSError("inventory unavailable")

    async def wait_for_change(revision):
        subscribed.set()
        return await original_wait(revision)

    monkeypatch.setattr(presence, "refresh", refresh)
    monkeypatch.setattr(presence, "wait_for_change", wait_for_change)
    waiter = asyncio.create_task(client.call("jobs.await", handles=[row["id"]], max_wait=2))
    try:
        await asyncio.wait_for(subscribed.wait(), timeout=1)
        assert not waiter.done()
        broken = False
        presence.set(row["id"], ABSENT)
        entries = await asyncio.wait_for(waiter, timeout=1)
        assert entries[0]["await_reason"] == "presence_released"
        assert entries[0]["human_presence"]["state"] == "absent"
    finally:
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
