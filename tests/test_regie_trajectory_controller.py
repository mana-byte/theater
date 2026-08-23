from __future__ import annotations

import asyncio

import pytest

from theater.regie.trajectory.controller import TrajectoryController
from theater.trajectory import PanelState


def wire_record(
    record_id: str,
    *,
    participant_id: str,
    revision: int = 1,
    summary: str | None = None,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "revision": revision,
        "participant_id": participant_id,
        "source_epoch": "epoch",
        "lane": "model",
        "kind": "assistant",
        "source": "claude",
        "summary": summary or record_id,
        "status": "completed",
        "turn_id": "turn-1",
    }


def page(
    participant_id: str,
    record_id: str,
    *,
    cursor: str = "c1",
    older: bool = False,
    records: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "panel_state": {
            "state": "ready",
            "participant_state": "live",
        },
        "stream_id": f"stream-{participant_id}",
        "cursor": cursor,
        "older_cursor": "older-1" if older else None,
        "has_older": older,
        "records": records
        or [
            wire_record(record_id, participant_id=participant_id)
            | {"raw_index": 0 if record_id == "old" else 1}
        ],
        "groups": [],
    }


class FakeClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def call(self, method: str, **params: object):
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

    async def query_handler(_method: str, params: dict[str, object]):
        if params["id"] == "a":
            await release_a.wait()
            return page("a", "old")
        return page("b", "current")

    query = FakeClient(query_handler)
    follow = FakeClient(lambda _method, _params: {"stream_id": "stream-b", "upserts": []})
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

    def query_handler(_method: str, _params: dict[str, object]):
        return responses.pop(0)

    query = FakeClient(query_handler)
    follow = FakeClient(lambda _method, _params: {"stream_id": "stream-p1", "upserts": []})
    controller = TrajectoryController(query, follow)
    await controller.open("p1", start_follow=False)
    assert await controller.load_older("p1") is not None
    assert await controller.load_older("p1") is None
    assert [*controller.state_for("p1").records] == ["old", "new"]
    assert len([call for call in query.calls if call[0] == "trajectory.snapshot"]) == 2
    await controller.close()


@pytest.mark.asyncio
async def test_load_older_clears_after_stale_generation_exception() -> None:
    release = asyncio.Event()

    async def query_handler(_method: str, params: dict[str, object]) -> object:
        if params["id"] == "a" and params.get("before") == "older-1":
            await release.wait()
            raise RuntimeError("late page")
        return page("a", "record", older=True) if params["id"] == "a" else page("b", "record")

    query = FakeClient(query_handler)
    follow = FakeClient(lambda _method, _params: {"stream_id": "stream-b", "upserts": []})
    controller = TrajectoryController(query, follow)
    await controller.open("a", start_follow=False)
    loading = asyncio.create_task(controller.load_older("a"))
    await asyncio.sleep(0)
    assert controller.state_for("a").loading_older
    await controller.open("b", start_follow=False)
    release.set()
    assert await loading is None
    assert not controller.state_for("a").loading_older
    await controller.close()


@pytest.mark.asyncio
async def test_mixed_participant_snapshot_is_rejected_without_repaint() -> None:
    mixed = page(
        "p1",
        "first",
        records=[
            wire_record("first", participant_id="p1"),
            wire_record("wrong", participant_id="p2"),
        ],
    )
    query = FakeClient(lambda _method, _params: mixed)
    follow = FakeClient(lambda _method, _params: {"stream_id": "stream-p1", "upserts": []})
    controller = TrajectoryController(query, follow)

    assert await controller.open("p1", start_follow=False) is None
    state = controller.state_for("p1")
    assert state.panel.state is PanelState.STALE
    assert not state.records
    await controller.close()


@pytest.mark.asyncio
async def test_follow_rejects_mixed_upsert_and_applies_valid_revision() -> None:
    follow_results: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    query = FakeClient(lambda _method, _params: page("p1", "first"))

    async def follow_handler(_method: str, _params: dict[str, object]):
        return await follow_results.get()

    follow = FakeClient(follow_handler)
    controller = TrajectoryController(query, follow, follow_wait=0)
    await controller.open("p1", start_follow=False)
    await controller.start_follow("p1")
    await follow_results.put(
        {
            "stream_id": "stream-p1",
            "cursor": "c2",
            "upserts": [
                {"record": wire_record("first", participant_id="p1", revision=2, summary="new")}
            ],
        }
    )
    await asyncio.sleep(0)
    assert controller.state_for("p1").records["first"].summary == "new"
    await follow_results.put(
        {
            "stream_id": "stream-p1",
            "upserts": [{"record": wire_record("wrong", participant_id="p2")}],
        }
    )
    await asyncio.sleep(0)
    assert controller.state_for("p1").panel.state is PanelState.STALE
    await controller.close()


@pytest.mark.asyncio
async def test_follow_preserves_paused_tail_and_resync_marks_stale() -> None:
    follow_results: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    query = FakeClient(lambda _method, _params: page("p1", "first"))

    async def follow_handler(_method: str, _params: dict[str, object]):
        return await follow_results.get()

    follow = FakeClient(follow_handler)
    controller = TrajectoryController(query, follow, follow_wait=0)
    await controller.open("p1", start_follow=False)
    controller.state_for("p1").pause_follow()
    await controller.start_follow("p1")
    await follow_results.put(
        {
            "stream_id": "stream-p1",
            "cursor": "c2",
            "upserts": [{"record": wire_record("second", participant_id="p1")}],
        }
    )
    await asyncio.sleep(0)
    state = controller.state_for("p1")
    await follow_results.put(
        {"stream_id": "stream-p1", "resync_required": True, "reason": "epoch changed"}
    )
    await asyncio.sleep(0)

    assert state.selected_id == "first"
    assert state.new_count == 1
    assert state.panel.state is PanelState.STALE
    assert state.retry_kind == "resync"
    await controller.close()
    assert query.closed and follow.closed


@pytest.mark.asyncio
async def test_controller_uses_best_effort_close_hints_on_switch_lru_and_final_close() -> None:
    query = FakeClient(lambda _method, params: page(str(params["id"]), "record"))
    follow = FakeClient(lambda _method, _params: {"stream_id": "stream", "upserts": []})
    controller = TrajectoryController(query, follow)
    await controller.open("a", start_follow=False)
    await controller.open("b", start_follow=False)
    await asyncio.sleep(0)
    await controller.close()

    closed_ids = [params["id"] for method, params in query.calls if method == "trajectory.close"]
    assert "a" in closed_ids and "b" in closed_ids
    assert query.closed and follow.closed
