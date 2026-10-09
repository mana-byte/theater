"""Daemon-side long-poll over bus appends: blocks, wakes fast, times out, filters kinds."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import insert

from theater.daemon.events.bus import _BUS_SCAN_WINDOW, BusTailWaiter
from theater.daemon.schema import bus


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
    started = time.perf_counter()
    new_id = daemon.store.bus_append("t.new")
    tail = await asyncio.wait_for(task, 1)
    assert time.perf_counter() - started < 0.05
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
    tail = await asyncio.wait_for(task, 1)
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
    assert (await asyncio.wait_for(task, 1)).rows == []


async def test_public_handler_long_polls_and_validates(daemon):
    from theater.daemon.frontend.diagnostic_handlers import bus_tail
    from theater.models import BadRequest

    after = daemon.store.bus_append("t.seed")
    task = asyncio.create_task(bus_tail(daemon, None, {"after_id": after, "wait_seconds": 5}))
    while daemon.bus_tail_waiter.waiter_count == 0:
        await asyncio.sleep(0)
    new_id = daemon.store.bus_append("t.new")
    result = await asyncio.wait_for(task, 1)
    assert result["next_after_id"] == new_id and result["next_cursor"] == str(new_id)
    for bad in ({"wait_seconds": 31}, {"wait_seconds": float("nan")}, {"kinds": "x"}):
        with pytest.raises(BadRequest):
            await bus_tail(daemon, None, bad)
