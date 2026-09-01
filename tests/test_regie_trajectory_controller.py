from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from theater.regie.trajectory import controller as controller_module
from theater.regie.trajectory.controller import TrajectoryController
from theater.regie.trajectory.state import TrajectoryStateStore
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryLocationResolution,
    TrajectoryParticipantState,
)


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


def test_controller_keeps_an_empty_injected_state_store() -> None:
    states = TrajectoryStateStore(page_size=17)
    query = FakeClient(lambda _method, _params: {})
    follow = FakeClient(lambda _method, _params: {})

    controller = TrajectoryController(query, follow, state_store=states)

    assert controller.state_store is states
    assert controller.state_store.page_size == 17


@pytest.mark.asyncio
async def test_controller_decodes_exact_record_location() -> None:
    response = {
        "participant_id": "p1",
        "requested_record_id": "bus:7",
        "resolution": "exact",
        "record": wire_record("bus:7", participant_id="p1"),
        "message": "",
    }
    query = FakeClient(lambda _method, _params: response)
    follow = FakeClient(lambda _method, _params: {})
    controller = TrajectoryController(query, follow)

    location = await controller.locate("p1", "bus:7")

    assert location.resolution is TrajectoryLocationResolution.EXACT
    assert location.record is not None and location.record.record_id == "bus:7"
    assert query.calls == [("trajectory.locate", {"id": "p1", "record_id": "bus:7"})]


@pytest.mark.asyncio
async def test_controller_applies_full_history_search_hits() -> None:
    def query_handler(method: str, _params: dict[str, object]) -> object:
        if method == "trajectory.snapshot":
            return page("p1", "recent")
        return {
            "query": "grafna",
            "records": [wire_record("old-grafana", participant_id="p1")],
            "scanned_records": 900,
            "matched_records": 1,
            "complete": True,
        }

    query = FakeClient(query_handler)
    follow = FakeClient(lambda _method, _params: {})
    controller = TrajectoryController(query, follow)
    await controller.open("p1", start_follow=False)

    await controller.search_full_history("grafna", "p1")

    state = controller.state_for("p1")
    assert [record.record_id for record in state.remote_search_records] == ["old-grafana"]
    assert state.search_scanned_records == 900
    assert state.search_complete is True
    assert query.calls[-1] == (
        "trajectory.search",
        {"id": "p1", "query": "grafna", "limit": 200},
    )
    await controller.close()


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
async def test_follow_resyncs_once_and_replaces_the_follow_loop() -> None:
    follow_results: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    resnapshot_requested = asyncio.Event()
    replacement_follow_started = asyncio.Event()
    snapshots = 0

    def query_handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            resnapshot_requested.set()
        return page("p1", "first")

    query = FakeClient(query_handler)
    follows = 0

    async def follow_handler(_method: str, _params: dict[str, object]):
        nonlocal follows
        follows += 1
        if follows == 2:
            replacement_follow_started.set()
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
    await resnapshot_requested.wait()
    await replacement_follow_started.wait()

    assert state.selected_id == "first"
    assert state.new_count == 1
    assert state.panel.state is PanelState.READY
    assert state.retry_kind is None
    assert snapshots == 2
    assert follows == 2
    await controller.close()
    assert query.closed and follow.closed


@pytest.mark.asyncio
async def test_failed_automatic_resync_retains_records_and_exposes_retry() -> None:
    follow_results: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    snapshots = 0
    failed = asyncio.Event()

    def query_handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            raise RuntimeError("fresh snapshot failed")
        return page("p1", "first")

    query = FakeClient(query_handler)

    async def follow_handler(_method: str, _params: dict[str, object]):
        return await follow_results.get()

    follow = FakeClient(follow_handler)
    controller = TrajectoryController(query, follow, follow_wait=0)
    controller.subscribe(
        lambda state: failed.set() if state.retry_kind == "resync" and not state.resyncing else None
    )
    await controller.open("p1")
    await follow_results.put({"stream_id": "stream-p1", "resync_required": True})
    await failed.wait()

    state = controller.state_for("p1")
    assert [*state.records] == ["first"]
    assert state.retry_kind == "resync"
    assert "failed" in state.retry_message
    assert snapshots == 2
    await controller.close()


@pytest.mark.asyncio
async def test_stale_generation_resync_cannot_repaint_another_participant() -> None:
    follow_results: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()
    snapshots = 0

    async def query_handler(_method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal snapshots
        if params["id"] == "a":
            snapshots += 1
            if snapshots == 2:
                snapshot_started.set()
                await release_snapshot.wait()
            return page("a", "a-record")
        return page("b", "b-record")

    query = FakeClient(query_handler)

    async def follow_handler(_method: str, _params: dict[str, object]):
        return await follow_results.get()

    follow = FakeClient(follow_handler)
    controller = TrajectoryController(query, follow, follow_wait=0)
    await controller.open("a")
    await follow_results.put({"stream_id": "stream-a", "resync_required": True})
    await snapshot_started.wait()
    await controller.open("b", start_follow=False)
    release_snapshot.set()

    assert controller.active_participant == "b"
    assert [*controller.state_for("b").records] == ["b-record"]
    await controller.close()


@pytest.mark.asyncio
async def test_panel_only_follow_delta_updates_state_and_stops_following(monkeypatch) -> None:
    applied = asyncio.Event()
    delta = SimpleNamespace(
        stream_id="stream-p1",
        cursor="c2",
        upserts=(),
        panel_state=PanelStateInfo(PanelState.WAITING, participant_state="dead"),
        resync_required=False,
        reason=None,
    )
    monkeypatch.setattr(controller_module, "decode_delta", lambda _value: delta)
    query = FakeClient(lambda _method, _params: page("p1", "first"))
    follow = FakeClient(lambda _method, _params: object())
    controller = TrajectoryController(query, follow, follow_wait=0)
    controller.subscribe(
        lambda state: (
            applied.set()
            if state.panel.participant_state is TrajectoryParticipantState.DEAD
            else None
        )
    )

    await controller.open("p1")
    task = controller.follow_task
    assert task is not None
    await applied.wait()
    await task

    state = controller.state_for("p1")
    assert [*state.records] == ["first"]
    assert state.panel.state is PanelState.WAITING
    assert state.panel.participant_state is TrajectoryParticipantState.DEAD
    assert len(follow.calls) == 1
    await controller.close()


def test_reset_keeps_only_a_pending_resync_retry() -> None:
    state = TrajectoryStateStore().get("p1")
    state.mark_retry("older", "older page failed")
    state.loading_older = True
    state.reload_required = True
    state.query = "query"
    state.detail_id = "record"
    state.reset_ui()

    assert state.retry_kind is None
    assert state.retry_message == ""
    assert state.query == ""
    assert state.detail_id is None
    assert state.loading_older
    assert state.reload_required

    state.mark_resync("cursor rejected")
    state.reset_ui()

    assert state.retry_kind == "resync"
    assert state.retry_message == "cursor rejected"
    assert state.reload_required


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
    assert {
        params.get("stream_id") for method, params in query.calls if method == "trajectory.close"
    } >= {"stream-a", "stream-b"}
    assert query.closed and follow.closed


@pytest.mark.asyncio
async def test_follow_does_not_start_without_stream_and_cursor() -> None:
    response = page("p1", "record") | {"cursor": None}
    query = FakeClient(lambda _method, _params: response)
    follow = FakeClient(lambda _method, _params: pytest.fail("follow should not run"))
    controller = TrajectoryController(query, follow)

    await controller.open("p1")

    assert controller.follow_task is None
    assert not await controller.start_follow("p1")
    await controller.close()


@pytest.mark.asyncio
async def test_follow_does_not_start_for_a_dead_participant() -> None:
    response = page("p1", "record")
    response["panel_state"] = {"state": "ready", "participant_state": "dead"}
    query = FakeClient(lambda _method, _params: response)
    follow = FakeClient(lambda _method, _params: pytest.fail("follow should not run"))
    controller = TrajectoryController(query, follow)

    await controller.open("p1")

    assert controller.follow_task is None
    assert not await controller.start_follow("p1")
    await controller.close()


@pytest.mark.asyncio
async def test_resume_resyncs_a_rejected_stream_before_following() -> None:
    query = FakeClient(lambda _method, _params: page("p1", "record"))
    release = asyncio.Event()

    async def follow_handler(_method: str, _params: dict[str, object]) -> dict[str, object]:
        await release.wait()
        return {"stream_id": "stream-p1", "upserts": []}

    follow = FakeClient(follow_handler)
    controller = TrajectoryController(query, follow)
    await controller.open("p1", start_follow=False)
    state = controller.state_for("p1")
    state.mark_resync("stream expired")
    state.reset_ui()

    assert state.retry_kind == "resync"
    assert await controller.resume_follow("p1")
    assert [method for method, _params in query.calls].count("trajectory.snapshot") == 2
    assert controller.follow_task is not None
    await controller.close()
