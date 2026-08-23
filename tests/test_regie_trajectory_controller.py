from __future__ import annotations

import asyncio

import pytest

from theater.regie.trajectory.controller import TrajectoryController
from theater.regie.trajectory.models import PanelStatus


def page(participant_id: str, record_id: str, *, cursor: str = "c1", older: bool = False) -> dict:
    return {
        "participant_id": participant_id,
        "panel": "ready",
        "stream_id": f"stream-{participant_id}",
        "cursor": cursor,
        "older_cursor": "older-1" if older else None,
        "has_older": older,
        "records": [
            {
                "record_id": record_id,
                "revision": 1,
                "participant_id": participant_id,
                "lane": "model",
                "kind": "assistant",
                "source": "claude",
                "summary": record_id,
                "status": "completed",
            }
        ],
    }


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        result = self.handler(method, params)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_generation_guard_rejects_old_participant_result() -> None:
    release_a = asyncio.Event()

    async def query_handler(method: str, params: dict):
        if params["id"] == "a":
            await release_a.wait()
            return page("a", "old")
        return page("b", "current")

    query = FakeClient(query_handler)
    follow = FakeClient(lambda _method, _params: {"participant_id": "b", "upserts": []})
    controller = TrajectoryController(query, follow)
    first = asyncio.create_task(controller.open("a", start_follow=False))
    await asyncio.sleep(0)
    await controller.open("b", start_follow=False)
    release_a.set()
    await first

    assert controller.active_participant == "b"
    assert [*controller.state_for("b").records] == ["current"]
    assert not controller.state_for("a").records
    await controller.close()


@pytest.mark.asyncio
async def test_one_page_older_loading_and_revision_precedence() -> None:
    responses = [page("p1", "new", cursor="c2", older=True), page("p1", "old")]

    def query_handler(_method: str, _params: dict):
        return responses.pop(0)

    query = FakeClient(query_handler)
    follow = FakeClient(lambda _method, _params: {"participant_id": "p1", "upserts": []})
    controller = TrajectoryController(query, follow)
    await controller.open("p1", start_follow=False)
    assert await controller.load_older("p1") is not None
    assert await controller.load_older("p1") is None
    assert [*controller.state_for("p1").records] == ["old", "new"]
    assert len([call for call in query.calls if call[0] == "trajectory.snapshot"]) == 2
    await controller.close()


@pytest.mark.asyncio
async def test_follow_preserves_paused_tail_and_resync_marks_stale() -> None:
    follow_results: asyncio.Queue[dict] = asyncio.Queue()
    query = FakeClient(lambda _method, _params: page("p1", "first"))

    async def follow_handler(_method: str, _params: dict):
        return await follow_results.get()

    follow = FakeClient(follow_handler)
    controller = TrajectoryController(query, follow, follow_wait=0)
    await controller.open("p1", start_follow=False)
    controller.state_for("p1").pause_follow()
    await controller.start_follow("p1")
    await follow_results.put(
        {
            "participant_id": "p1",
            "cursor": "c2",
            "upserts": [
                {
                    "record_id": "second",
                    "revision": 1,
                    "participant_id": "p1",
                    "lane": "model",
                    "kind": "assistant",
                    "source": "claude",
                    "summary": "second",
                    "status": "completed",
                }
            ],
        }
    )
    await asyncio.sleep(0)
    state = controller.state_for("p1")
    await follow_results.put(
        {"participant_id": "p1", "resync_required": True, "reason": "epoch changed"}
    )
    await asyncio.sleep(0)

    assert state.selected_id == "first"
    assert state.new_count == 1
    assert state.panel.status is PanelStatus.STALE
    assert state.retry_kind == "resync"
    await controller.close()
    assert query.closed and follow.closed
