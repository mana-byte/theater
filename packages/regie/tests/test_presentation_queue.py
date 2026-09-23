from __future__ import annotations

import asyncio

import pytest
from regie.controllers.presentation_queue import PresentationQueue


async def test_close_finishes_active_move_and_discards_only_unstarted_requests():
    queue = PresentationQueue()
    entered, release = asyncio.Event(), asyncio.Event()

    async def move():
        entered.set()
        await release.wait()
        return "moved"

    async def unexpected():
        raise AssertionError("shutdown must not start another pane move")

    first = queue.submit("stage", move)
    await entered.wait()
    second = queue.submit("stage", unexpected)
    closing = asyncio.create_task(queue.close())
    await asyncio.sleep(0)
    assert second.cancelled() and not closing.done() and not first.done()
    release.set()
    await closing
    assert await first == "moved"
    assert queue.submit("stage", unexpected).cancelled()


async def test_failed_action_does_not_reorder_or_strand_following_work():
    queue = PresentationQueue()
    seen = []

    async def failed():
        seen.append("failed")
        raise ValueError("stale identity")

    async def next_action():
        seen.append("next")
        return 2

    first = queue.submit("stage", failed)
    second = queue.submit("stage", next_action)
    with pytest.raises(ValueError, match="stale identity"):
        await first
    assert await second == 2
    assert seen == ["failed", "next"]
    await queue.close()


async def test_work_that_never_awaits_does_not_stall_the_queue_under_eager_tasks():
    # Textual installs eager_task_factory; a drain can then finish inside create_task.
    asyncio.get_running_loop().set_task_factory(asyncio.eager_task_factory)
    queue = PresentationQueue()

    async def immediate() -> str:
        return "done"

    try:
        assert await queue.submit("trajectory", immediate) == "done"
        assert await asyncio.wait_for(queue.submit("toggle", immediate), 1) == "done"
    finally:
        asyncio.get_running_loop().set_task_factory(None)
