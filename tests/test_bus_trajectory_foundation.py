"""Focused persistence tests for the trajectory coordination feed foundation."""

from __future__ import annotations

import asyncio

import pytest

from theater.constants.daemon import (
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
    BUS_PARTICIPANT_PAGE_MAX_LIMIT,
)
from theater.daemon.rpc import jobs as jobs_rpc
from theater.models import Participant


def test_bus_listener_is_post_commit_and_failure_isolated(store):
    seen: list[dict] = []

    def broken(_row):
        raise RuntimeError("listener failed")

    def capture(row):
        seen.append(dict(row))
        assert store.bus_tail(after_id=row["id"] - 1)[-1]["id"] == row["id"]

    store.register_bus_listener(broken)
    store.register_bus_listener(capture)
    row_id = store.bus_append(
        BUS_KIND_PARTICIPANT_KILL_REQUESTED,
        from_id="cli",
        to_id="target",
        payload={"safe": True},
    )

    assert row_id == seen[0]["id"]
    assert seen[0] == store.bus_tail(after_id=row_id - 1)[-1]
    store.unregister_bus_listener(broken)
    store.unregister_bus_listener(capture)
    store.bus_append("after.unregister")
    assert len(seen) == 1


def test_bus_listener_does_not_swallow_base_exceptions(store):
    def abort(_row):
        raise SystemExit("stop")

    store.register_bus_listener(abort)
    with pytest.raises(SystemExit, match="stop"):
        store.bus_append("listener.abort")

    assert store.bus_tail(limit=1)[0]["kind"] == "listener.abort"


def test_bus_listener_deep_copies_nested_payloads(store):
    seen: list[dict] = []

    def mutate(row):
        row["payload"]["nested"]["items"].append("mutated")
        row["payload"]["nested"]["flags"]["changed"] = True

    store.register_bus_listener(mutate)
    store.register_bus_listener(seen.append)
    store.bus_append(
        "listener.nested",
        payload={"nested": {"items": [1], "flags": {"original": True}}},
    )

    assert seen[0]["payload"] == {"nested": {"items": [1], "flags": {"original": True}}}


def test_atomic_bus_rows_notify_after_commit(store):
    prior = Participant(id="prior", harness="vibe")
    target = Participant(id="target", harness="vibe")
    store.upsert_participant(prior)
    store.upsert_participant(target)
    seen: list[dict] = []
    store.register_bus_listener(seen.append)

    store.bind_operator_transcript(
        target=target,
        prior_owner=prior,
        audit_payload={"path": "/tmp/messages.jsonl"},
    )

    assert [row["kind"] for row in seen] == [
        "operator.transcript_unbind",
        "operator.transcript_bind",
    ]
    assert [row["id"] for row in seen] == [row["id"] for row in store.bus_tail(limit=2)]


def test_participant_bus_pages_include_both_directions_and_filter_kinds(store):
    events = [
        store.bus_append("coord", from_id="a", to_id="a"),
        store.bus_append("coord", from_id="b", to_id="a"),
        store.bus_append("noise", from_id="a", to_id="b"),
        store.bus_append("coord", from_id="a", to_id="b"),
        store.bus_append("coord", from_id="c", to_id="a"),
        store.bus_append("coord", from_id="b", to_id="b"),
    ]

    page = store.bus_page_for_participant("a", kinds={"coord"}, limit=2)
    assert [row["id"] for row in page] == [events[3], events[4]]
    assert [row["id"] for row in store.bus_page_for_participant("b", kinds={"coord"})] == [
        events[1],
        events[3],
        events[5],
    ]
    older = store.bus_page_for_participant("a", kinds={"coord"}, before_id=events[3])
    assert [row["id"] for row in older] == [
        events[0],
        events[1],
    ]
    assert len(store.bus_page_for_participant("a", kinds={"coord"}, limit=10_000)) <= (
        BUS_PARTICIPANT_PAGE_MAX_LIMIT
    )


def test_bus_participant_indexes_are_compound(store):
    indexes = {
        row[1]: [info[2] for info in store.conn.exec_driver_sql(f"PRAGMA index_info('{row[1]}')")]
        for row in store.conn.exec_driver_sql("PRAGMA index_list('bus')")
    }
    assert indexes["idx_bus_from_id_id"] == ["from_id", "id"]
    assert indexes["idx_bus_to_id_id"] == ["to_id", "id"]


@pytest.mark.parametrize("limit", [True, -1, 1.5, "2", None])
def test_participant_bus_page_rejects_invalid_limits(store, limit):
    with pytest.raises(ValueError, match="limit"):
        store.bus_page_for_participant("a", kinds={"coord"}, limit=limit)


@pytest.mark.parametrize("before_id", [True, -1, 1.5, "not-a-cursor"])
def test_participant_bus_page_rejects_invalid_cursors(store, before_id):
    with pytest.raises(ValueError, match="before_id"):
        store.bus_page_for_participant("a", kinds={"coord"}, before_id=before_id)


async def test_kill_request_is_distinct_from_observed_death(client, fake_tmux):
    record = await client.call("spawn", harness="vibe", prompt="hi", approval="manual", cwd="/tmp")
    await client.call("participant.kill", id=record["id"])

    rows = [
        row
        for row in await client.call("bus.tail")
        if row["kind"] == BUS_KIND_PARTICIPANT_KILL_REQUESTED
    ]
    assert len(rows) == 1
    assert rows[0]["from_id"] == "cli"
    assert rows[0]["to_id"] == record["id"]


async def test_await_end_records_completed_and_elapsed(daemon, fake_tmux, monkeypatch):
    monkeypatch.setattr("theater.daemon.methods.AWAIT_ANNOUNCE_AFTER", 0.0)
    from theater.client import DaemonClient

    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        caller = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
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


async def test_await_end_outcome_is_per_handle(daemon, fake_tmux, monkeypatch):
    monkeypatch.setattr("theater.daemon.methods.AWAIT_ANNOUNCE_AFTER", 0.0)
    from theater.client import DaemonClient

    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        caller = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
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
            children[1]["handle"]: "timeout",
        }
    finally:
        await client.aclose()


async def test_await_elapsed_starts_at_announcement_and_is_nonnegative(
    daemon, fake_tmux, monkeypatch
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
        caller = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
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


async def test_cancelled_await_records_cancelled(daemon, fake_tmux, monkeypatch):
    monkeypatch.setattr("theater.daemon.methods.AWAIT_ANNOUNCE_AFTER", 0.0)
    from theater.client import DaemonClient

    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        caller = await client.call("hello", harness="vibe", pane="%2", cwd="/tmp")
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
