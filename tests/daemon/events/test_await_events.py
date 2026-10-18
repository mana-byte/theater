"""Awaits and spawns leave an accurate story on the bus."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.presence import FakePresence
from theater.daemon import methods
from theater.daemon.rpc import jobs as jobs_rpc
from theater.protocol import RemoteError


async def test_bus_records_the_story(client, terminal_provider):
    await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    kinds = [e["kind"] for e in await client.call("bus.tail")]
    assert "participant.created" in kinds
    assert "participant.pane" not in kinds


async def test_spawn_created_event_marks_whether_a_prompt_was_sent(client, terminal_provider):
    await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("spawn", harness="vibe", prompt="", approval="manual", cwd="/tmp")

    created = [e for e in await client.call("bus.tail") if e["kind"] == "participant.created"]
    assert [e["payload"]["has_prompt"] for e in created] == [True, False]


async def _await_events(client):
    return [e for e in await client.call("bus.tail") if e["kind"].startswith("job.await")]


async def test_await_records_active_wait_edges(client, terminal_provider, monkeypatch, daemon):
    # Patch the announce delay rather than sleep it out: every test below is
    # about *which* rows an await writes, and a wall-clock threshold is flaky
    # on a loaded machine. The one test about timing patches it too, on both
    # sides of the wait.
    daemon.presence = FakePresence()
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    jobs = await client.call(
        "jobs.await",
        handles=[child["handle"]],
        caller_id=parent["id"],
        max_wait=0.01,
    )

    assert jobs[0]["state"] == "running"
    await_events = await _await_events(client)
    assert [e["kind"] for e in await_events] == ["job.await.start", "job.await.end"]
    start, end = await_events
    assert start["from_id"] == parent["id"]
    assert start["to_id"] == child["id"]
    assert start["payload"]["handle"] == child["handle"]
    assert end["from_id"] == parent["id"]
    assert end["to_id"] == child["id"]
    assert end["payload"]["handle"] == start["payload"]["handle"]
    assert end["payload"]["token"] == start["payload"]["token"]
    assert end["payload"]["state"] == "timeout"
    assert end["payload"]["elapsed_seconds"] >= 0


async def test_await_records_one_pair_per_handle(client, terminal_provider, monkeypatch, daemon):
    """Two children, two edges — and every start closed exactly once."""
    daemon.presence = FakePresence()
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    children = [
        await client.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=parent["id"],
        )
        for _ in range(3)
    ]

    await client.call(
        "jobs.await",
        handles=[c["handle"] for c in children],
        caller_id=parent["id"],
        max_wait=0.01,
    )

    await_events = await _await_events(client)
    starts = [e for e in await_events if e["kind"] == "job.await.start"]
    ends = [e for e in await_events if e["kind"] == "job.await.end"]
    assert [e["payload"]["handle"] for e in starts] == [c["handle"] for c in children]
    assert [e["payload"]["handle"] for e in ends] == [c["handle"] for c in children]
    assert [e["to_id"] for e in starts] == [c["id"] for c in children]
    # One await, one token: the régie pairs an end to its start by it.
    assert len({e["payload"]["token"] for e in await_events}) == 1


async def test_await_that_returns_immediately_does_not_record_active_wait(
    client, terminal_provider, monkeypatch
):
    """A finished job is not something to be blocked on, delay or no delay."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    jobs = await client.call(
        "jobs.await",
        handles=[child["handle"]],
        caller_id=parent["id"],
        max_wait=1.0,
    )

    assert jobs[0]["state"] == "done"
    assert await _await_events(client) == []


async def test_await_with_one_finished_job_records_nothing(client, terminal_provider, monkeypatch):
    """One terminal job ends the whole call at entry — so no edge is live."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    running = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    # A promptless spawn has nothing to report, so its job is done on arrival.
    finished = await client.call(
        "spawn",
        harness="vibe",
        prompt="",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    await client.call(
        "jobs.await",
        handles=[running["handle"], finished["handle"]],
        caller_id=parent["id"],
        max_wait=1.0,
    )

    assert await _await_events(client) == []


async def test_short_await_does_not_announce(client, terminal_provider):
    """The polling case: `max_wait` under the threshold writes nothing.

    Runs against the real `AWAIT_ANNOUNCE_AFTER`, because the number is the
    point: an agent polling in a loop must not flood the bus, since `bus_tail`
    keeps only the newest rows and the flood would drop somebody else's
    `job.await.end`.
    """
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    for _ in range(3):
        await client.call(
            "jobs.await",
            handles=[child["handle"]],
            caller_id=parent["id"],
            max_wait=0.02,
        )

    assert await _await_events(client) == []


async def test_await_announces_once_it_has_really_blocked(client, terminal_provider, monkeypatch):
    """The threshold, not the call, is what puts a row on the bus."""
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )
    call = {
        "handles": [child["handle"]],
        "caller_id": parent["id"],
        "max_wait": 0.05,
    }

    # Threshold above the wait: the caller gave up before the régie would ever
    # have been told it was waiting.
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 5.0)
    await client.call("jobs.await", **call)
    assert await _await_events(client) == []

    # Same call, threshold under the wait; generous so load can't beat the blocked signal.
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    await client.call("jobs.await", **{**call, "max_wait": 1.0})
    # Wait for both rows rather than sample once: the bus flush can lag under load.
    async with asyncio.timeout(5):
        while len(events := await _await_events(client)) < 2:
            await asyncio.sleep(0.01)
    assert [e["kind"] for e in events] == ["job.await.start", "job.await.end"]


async def test_await_refused_by_the_rails_records_nothing(client, terminal_provider, monkeypatch):
    """A refused await never happened: no row for the régie to animate."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "jobs.await",
            handles=[child["handle"]],
            caller_id=child["id"],
            max_wait=1.0,
        )
    assert exc.value.code == "cycle_detected"
    assert await _await_events(client) == []


async def test_await_that_raises_still_closes_its_starts(
    daemon, client, terminal_provider, monkeypatch
):
    """An exception inside the wait must not strand the animation."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    child = await client.call(
        "spawn",
        harness="vibe",
        prompt="hi",
        approval="manual",
        cwd="/tmp",
        parent_id=parent["id"],
    )

    async def boom(handles, max_wait=150.0):
        # Outlast the (patched-to-zero) announce delay, so the start rows are
        # on the bus before the wait falls over.
        await asyncio.sleep(0.05)
        raise RuntimeError("the store fell over mid-wait")

    monkeypatch.setattr(daemon.jobs, "await_jobs", boom)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "jobs.await",
            handles=[child["handle"]],
            caller_id=parent["id"],
            max_wait=1.0,
        )
    assert exc.value.code == "internal"
    assert [e["kind"] for e in await _await_events(client)] == [
        "job.await.start",
        "job.await.end",
    ]


async def test_a_start_that_fails_halfway_still_closes_what_was_written(
    daemon, client, terminal_provider, monkeypatch
):
    """Half the start rows out, then the disk refuses — close those halves."""
    monkeypatch.setattr(methods, "AWAIT_ANNOUNCE_AFTER", 0.0)
    parent = await client.call("hello", harness="vibe", cwd="/tmp")
    children = [
        await client.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=parent["id"],
        )
        for _ in range(3)
    ]

    real_append = daemon.store.bus_append
    starts = 0

    def flaky_append(kind, **kwargs):
        nonlocal starts
        if kind == "job.await.start":
            starts += 1
            if starts > 1:
                raise OSError("disk full")
        return real_append(kind, **kwargs)

    monkeypatch.setattr(daemon.store, "bus_append", flaky_append)

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "jobs.await",
            handles=[c["handle"] for c in children],
            caller_id=parent["id"],
            max_wait=1.0,
        )
    assert exc.value.code == "internal"

    monkeypatch.setattr(daemon.store, "bus_append", real_append)
    await_events = await _await_events(client)
    assert [e["kind"] for e in await_events] == ["job.await.start", "job.await.end"]
    assert await_events[0]["payload"]["handle"] == await_events[1]["payload"]["handle"]
    assert await_events[0]["payload"]["token"] == await_events[1]["payload"]["token"]
    assert await_events[1]["payload"]["state"] == "error"
    assert await_events[1]["payload"]["elapsed_seconds"] >= 0


async def test_await_end_records_completed_and_elapsed(daemon, terminal_provider, monkeypatch):
    monkeypatch.setattr("theater.daemon.methods.AWAIT_ANNOUNCE_AFTER", 0.0)
    from theater.client import DaemonClient

    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        caller = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
        child = await client.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=caller["id"],
        )
        waiting = asyncio.create_task(
            jobs_rpc._jobs_await(
                daemon,
                {
                    "handles": [child["handle"]],
                    "caller_id": caller["id"],
                    "max_wait": 1.0,
                },
            )
        )
        for _ in range(100):
            if any(row["kind"] == "job.await.start" for row in daemon.store.bus_tail()):
                break
            await asyncio.sleep(0.001)
        daemon.jobs.finish(child["handle"], state="done", result="ok")
        await waiting
        end = next(row for row in daemon.store.bus_tail() if row["kind"] == "job.await.end")
        assert end["payload"]["state"] == "completed"
        assert end["payload"]["elapsed_seconds"] >= 0
    finally:
        await client.aclose()


async def test_await_end_outcome_is_per_handle(daemon, terminal_provider, monkeypatch):
    monkeypatch.setattr("theater.daemon.methods.AWAIT_ANNOUNCE_AFTER", 0.0)
    from theater.client import DaemonClient

    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        caller = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
        children = [
            await client.call(
                "spawn",
                harness="vibe",
                prompt="hi",
                approval="manual",
                cwd="/tmp",
                parent_id=caller["id"],
            )
            for _ in range(2)
        ]
        waiting = asyncio.create_task(
            jobs_rpc._jobs_await(
                daemon,
                {
                    "handles": [child["handle"] for child in children],
                    "caller_id": caller["id"],
                    "max_wait": 1.0,
                },
            )
        )
        for _ in range(100):
            starts = [row for row in daemon.store.bus_tail() if row["kind"] == "job.await.start"]
            if len(starts) == 2:
                break
            await asyncio.sleep(0.001)
        assert len(starts) == 2

        daemon.jobs.finish(children[0]["handle"], state="done", result="ok")
        result = await waiting
        assert {row["handle"]: row["state"] for row in result} == {
            children[0]["handle"]: "done",
            children[1]["handle"]: "running",
        }
        ends = {
            row["payload"]["handle"]: row["payload"]["state"]
            for row in daemon.store.bus_tail()
            if row["kind"] == "job.await.end"
        }
        assert ends == {
            children[0]["handle"]: "completed",
            children[1]["handle"]: "pending",
        }
    finally:
        await client.aclose()


@pytest.mark.parametrize("job_state", ["crashed", "killed"])
async def test_await_end_records_terminal_failures_as_errors(
    daemon, terminal_provider, monkeypatch, job_state
):
    monkeypatch.setattr("theater.daemon.methods.AWAIT_ANNOUNCE_AFTER", 0.0)
    from theater.client import DaemonClient

    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        caller = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
        child = await client.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=caller["id"],
        )
        waiting = asyncio.create_task(
            jobs_rpc._jobs_await(
                daemon,
                {
                    "handles": [child["handle"]],
                    "caller_id": caller["id"],
                    "max_wait": 1.0,
                },
            )
        )
        for _ in range(100):
            if any(row["kind"] == "job.await.start" for row in daemon.store.bus_tail()):
                break
            await asyncio.sleep(0.001)
        daemon.jobs.finish(child["handle"], state=job_state, result="failed")
        await waiting
        end = next(row for row in daemon.store.bus_tail() if row["kind"] == "job.await.end")
        assert end["payload"]["state"] == "error"
    finally:
        await client.aclose()


async def test_await_elapsed_starts_at_announcement_and_is_nonnegative(
    daemon, terminal_provider, monkeypatch
):
    monkeypatch.setattr("theater.daemon.methods.AWAIT_ANNOUNCE_AFTER", 0.0)
    from theater.client import DaemonClient

    clock = [10.0]

    class Clock:
        def monotonic(self):
            return clock[0]

    monkeypatch.setattr(jobs_rpc, "time", Clock())
    real_append = daemon.store.bus_append

    def append(kind, **kwargs):
        if kind == "job.await.start":
            clock[0] += 5.0
        return real_append(kind, **kwargs)

    monkeypatch.setattr(daemon.store, "bus_append", append)
    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        caller = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
        child = await client.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=caller["id"],
        )
        waiting = asyncio.create_task(
            jobs_rpc._jobs_await(
                daemon,
                {
                    "handles": [child["handle"]],
                    "caller_id": caller["id"],
                    "max_wait": 1.0,
                },
            )
        )
        for _ in range(100):
            if any(row["kind"] == "job.await.start" for row in daemon.store.bus_tail()):
                break
            await asyncio.sleep(0.001)
        clock[0] += 1.0
        daemon.jobs.finish(child["handle"], state="done", result="ok")
        await waiting
        end = next(row for row in daemon.store.bus_tail() if row["kind"] == "job.await.end")
        assert end["payload"]["elapsed_seconds"] == 1.0

        clock[0] -= 2.0
        child = await client.call(
            "spawn",
            harness="vibe",
            prompt="again",
            approval="manual",
            cwd="/tmp",
            parent_id=caller["id"],
        )
        waiting = asyncio.create_task(
            jobs_rpc._jobs_await(
                daemon,
                {
                    "handles": [child["handle"]],
                    "caller_id": caller["id"],
                    "max_wait": 1.0,
                },
            )
        )
        for _ in range(100):
            starts = [
                row
                for row in daemon.store.bus_tail()
                if row["kind"] == "job.await.start" and row["payload"]["handle"] == child["handle"]
            ]
            if starts:
                break
            await asyncio.sleep(0.001)
        clock[0] -= 2.0
        daemon.jobs.finish(child["handle"], state="done", result="ok")
        await waiting
        end = next(
            row
            for row in daemon.store.bus_tail()
            if row["kind"] == "job.await.end" and row["payload"]["handle"] == child["handle"]
        )
        assert end["payload"]["elapsed_seconds"] == 0.0
    finally:
        await client.aclose()


async def test_cancelled_await_records_cancelled(daemon, terminal_provider, monkeypatch):
    monkeypatch.setattr("theater.daemon.methods.AWAIT_ANNOUNCE_AFTER", 0.0)
    from theater.client import DaemonClient

    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        caller = await client.call("hello", harness="vibe", pane=None, cwd="/tmp")
        child = await client.call(
            "spawn",
            harness="vibe",
            prompt="hi",
            approval="manual",
            cwd="/tmp",
            parent_id=caller["id"],
        )
        waiting = asyncio.create_task(
            jobs_rpc._jobs_await(
                daemon,
                {
                    "handles": [child["handle"]],
                    "caller_id": caller["id"],
                    "max_wait": 10.0,
                },
            )
        )
        for _ in range(100):
            if any(row["kind"] == "job.await.start" for row in daemon.store.bus_tail()):
                break
            await asyncio.sleep(0.001)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        end = next(row for row in daemon.store.bus_tail() if row["kind"] == "job.await.end")
        assert end["payload"]["state"] == "cancelled"
        assert end["payload"]["elapsed_seconds"] >= 0
    finally:
        await client.aclose()
