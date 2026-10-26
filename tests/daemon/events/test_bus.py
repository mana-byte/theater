"""Bus listeners, participant pages and the long-poll tail."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import insert

from theater.constants.daemon import (
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
    BUS_PARTICIPANT_PAGE_MAX_LIMIT,
)
from theater.daemon.events.bus import _BUS_SCAN_WINDOW, BusTailWaiter
from theater.daemon.persistence.store import Store
from theater.daemon.schema import bus
from theater.models import Participant


@pytest.fixture
async def waiter(daemon):
    waiter = BusTailWaiter(daemon.store)
    yield waiter
    await waiter.aclose()


async def test_blocks_then_wakes_on_append(daemon, waiter):
    after = daemon.store.bus_append("t.seed")
    task = asyncio.create_task(waiter.tail(after, 10, None, 10.0))
    while waiter.waiter_count == 0:
        await asyncio.sleep(0)
    assert not task.done()
    new_id = daemon.store.bus_append("t.new")
    tail = await asyncio.wait_for(task, 5)  # woken by the append, not by the 10s deadline
    assert [row["id"] for row in tail.rows] == [new_id]
    assert tail.next_after_id == new_id


async def test_times_out_empty_and_zero_wait_does_not_block(daemon, waiter):
    after = daemon.store.bus_append("t.seed")
    assert (await waiter.tail(after, 10, None, 0.0)).rows == []
    tail = await waiter.tail(after, 10, None, 0.05)
    assert tail.rows == [] and tail.next_after_id == after


async def test_kinds_keeps_waiting_and_advances_cursor_past_filtered_rows(daemon, waiter):
    after = daemon.store.bus_append("t.seed")
    task = asyncio.create_task(waiter.tail(after, 10, ["t.want"], 10.0))
    while waiter.waiter_count == 0:
        await asyncio.sleep(0)
    skipped = daemon.store.bus_append("t.other")
    await asyncio.sleep(0.02)
    assert not task.done()
    wanted = daemon.store.bus_append("t.want")
    tail = await asyncio.wait_for(task, 5)
    assert [row["id"] for row in tail.rows] == [wanted] and skipped < wanted
    daemon.store.bus_append("t.other")
    only_other = await waiter.tail(wanted, 10, ["t.want"], 0.0)
    assert only_other.rows == [] and only_other.next_after_id > wanted


async def test_kinds_filter_before_truncation_not_after(daemon, waiter):
    after = daemon.store.bus_append("t.seed")
    wanted = daemon.store.bus_append("t.want")
    for _ in range(500):
        daemon.store.bus_append("t.other")
    tail = await waiter.tail(after, 10, ["t.want"], 0.0)
    assert [row["id"] for row in tail.rows] == [wanted]
    assert tail.next_after_id == wanted + 500  # every scanned row was consumed
    assert (await waiter.tail(tail.next_after_id, 10, ["t.want"], 0.0)).rows == []


async def test_expired_deadline_stops_between_windows_on_sparse_history(daemon, waiter):
    after = daemon.store.bus_append("t.seed")
    far = 10_000_000
    daemon.store._db.conn.execute(insert(bus).values(id=far, ts=0.0, kind="t.want"))
    tail = await waiter.tail(after, 10, ["t.want"], 0.0)
    assert tail.rows == [] and tail.next_after_id == after + _BUS_SCAN_WINDOW
    resumed = await waiter.tail(tail.next_after_id, 10, ["t.want"], 30.0)
    assert [row["id"] for row in resumed.rows] == [far]


async def test_close_wakes_waiters(daemon, waiter):
    after = daemon.store.bus_append("t.seed")
    task = asyncio.create_task(waiter.tail(after, 10, None, 10.0))
    while waiter.waiter_count == 0:
        await asyncio.sleep(0)
    await waiter.aclose()
    assert (await asyncio.wait_for(task, 5)).rows == []


async def test_public_handler_long_polls_and_validates(daemon):
    from theater.daemon.frontend.diagnostic_handlers import bus_tail
    from theater.models import BadRequest

    after = daemon.store.bus_append("t.seed")
    task = asyncio.create_task(bus_tail(daemon, None, {"after_id": after, "wait_seconds": 5}))
    while daemon.bus_tail_waiter.waiter_count == 0:
        await asyncio.sleep(0)
    new_id = daemon.store.bus_append("t.new")
    result = await asyncio.wait_for(task, 5)
    assert result["next_after_id"] == new_id and result["next_cursor"] == str(new_id)
    for bad in ({"wait_seconds": 31}, {"wait_seconds": float("nan")}, {"kinds": "x"}):
        with pytest.raises(BadRequest):
            await bus_tail(daemon, None, bad)


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


@pytest.mark.parametrize("limit", [True, -1, 1.5, "2", None])
def test_participant_bus_page_rejects_invalid_limits(store, limit):
    with pytest.raises(ValueError, match="limit"):
        store.bus_page_for_participant("a", kinds={"coord"}, limit=limit)


@pytest.mark.parametrize("before_id", [True, -1, 1.5, "not-a-cursor"])
def test_participant_bus_page_rejects_invalid_cursors(store, before_id):
    with pytest.raises(ValueError, match="before_id"):
        store.bus_page_for_participant("a", kinds={"coord"}, before_id=before_id)


async def test_kill_request_is_distinct_from_observed_death(client, terminal_provider):
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


def test_external_transaction_cannot_notify_before_commit(tmp_path) -> None:
    store = Store(tmp_path / "external.db")
    heard: list[dict] = []
    store.register_bus_listener(heard.append)
    try:
        with pytest.raises(RuntimeError, match="external transaction"):  # noqa: SIM117
            with store.engine.begin() as connection:
                store.bus_append("agent.assistant", from_id="participant-a", connection=connection)

        assert heard == []
        assert store.bus_tail() == []
    finally:
        store.close()
