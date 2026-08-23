from __future__ import annotations

import asyncio

import pytest
from rich.console import Console
from textual.app import App, ComposeResult

from theater.regie.trajectory.constants import LEDGER_OVERSCAN_ROWS, TIMELINE_PADDING
from theater.regie.trajectory.controller import TrajectoryController
from theater.regie.trajectory.filter_panel import FilterPanel
from theater.regie.trajectory.inspector import Inspector
from theater.regie.trajectory.ledger import Ledger
from theater.regie.trajectory.models import (
    FilterDimension,
    InspectorTab,
    PanelInfo,
    PanelStatus,
    TrajectoryFollow,
    TrajectoryPage,
    TrajectoryRecord,
    WireDecodeError,
)
from theater.regie.trajectory.render import (
    inspector_content,
    record_line,
    sanitize_text,
    tabs_for_record,
)
from theater.regie.trajectory.search import search_records
from theater.regie.trajectory.timeline import Timeline, TimelineSpanClicked
from theater.regie.trajectory.view import TrajectoryView


def wire_record(
    record_id: str,
    *,
    participant_id: str = "p1",
    index: int = 0,
    turn_id: str | None = "t1",
    lane: str = "model",
    kind: str = "assistant",
    summary: str | None = None,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "revision": 1,
        "participant_id": participant_id,
        "lane": lane,
        "kind": kind,
        "source": "claude",
        "summary": summary if summary is not None else f"record {index}",
        "status": "completed",
        "turn_id": turn_id,
    }


def record(record_id: str, *, index: int = 0, turn_id: str | None = "t1") -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(wire_record(record_id, index=index, turn_id=turn_id))


def page(participant_id: str, record_id: str) -> dict[str, object]:
    return {
        "participant_id": participant_id,
        "panel": "ready",
        "stream_id": f"stream-{participant_id}",
        "cursor": "cursor",
        "records": [wire_record(record_id, participant_id=participant_id)],
    }


class Host(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1", id="trajectory")


async def populate(app: Host, records: list[TrajectoryRecord]) -> TrajectoryView:
    view = app.query_one(TrajectoryView)
    view.state.panel = PanelInfo(PanelStatus.READY)
    view.state.upsert(records)
    view._refresh()
    return view


async def test_ledger_window_is_bounded_and_scroll_hit_testing_uses_offset() -> None:
    records = [record(f"r{index}", index=index, turn_id=f"t{index}") for index in range(100)]
    app = Host()
    async with app.run_test(size=(100, 30)):
        await populate(app, records)
        ledger = app.query_one(Ledger)
        ledger._viewport_height = 5
        ledger.update_rows(records, search_records(records))

        assert ledger.rendered_record_count <= 5 + 2 * LEDGER_OVERSCAN_ROWS
        ledger.set_scroll_offset(10)
        assert ledger._entry_at(1).record_id == "r5"
        assert ledger.rendered_record_count <= 5 + 2 * LEDGER_OVERSCAN_ROWS


async def test_ledger_prepend_preserves_selected_anchor() -> None:
    original = [record(f"r{index}", index=index) for index in range(20)]
    app = Host()
    async with app.run_test(size=(100, 30)):
        await populate(app, original)
        ledger = app.query_one(Ledger)
        ledger._viewport_height = 5
        ledger.update_rows(original, search_records(original), selected_id="r10")
        ledger.set_scroll_offset(5)
        old_offset = ledger._scroll_offset

        older = [record(f"old{index}", index=index, turn_id="older") for index in range(3)]
        combined = older + original
        ledger.update_rows(combined, search_records(combined), selected_id="r10")

        assert ledger._scroll_offset == old_offset + 4


async def test_timeline_hit_testing_scroll_selection_and_one_cell_per_record() -> None:
    records = [record(f"r{index}", index=index) for index in range(10)]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        timeline = view.query_one(Timeline)
        timeline._viewport_width = 4
        timeline.set_scroll_offset(5)

        assert timeline._record_at(TIMELINE_PADDING).record_id == "r5"
        assert timeline.scroll_span_into_view("r9") == 8
        assert len(timeline._render_timeline().plain) == len(records)
        timeline._hovered_id = "r9"
        assert "reverse" in str(timeline._render_timeline().get_style_at_offset(Console(), 9))


def test_search_cache_and_hover_path_do_not_recompute_search() -> None:
    async def scenario() -> None:
        app = Host()
        async with app.run_test(size=(100, 30)):
            view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
            key = view._search_key
            cache_sizes = (len(view._search_cache.corpus), len(view._search_cache.query_scores))
            view.on_ledger_record_hovered(type("Hover", (), {"record_id": "r1"})())
            assert view._search_key == key
            assert (
                len(view._search_cache.corpus),
                len(view._search_cache.query_scores),
            ) == cache_sizes

    asyncio.run(scenario())


async def test_search_input_keeps_printable_navigation_keys() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)) as pilot:
        view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
        view.action_open_search()
        await pilot.press(*"jklfdr y")
        assert view.state.query == "jklfdr y"


async def test_filter_panel_has_selectable_counts_and_filters_records() -> None:
    records = [
        TrajectoryRecord.from_wire(wire_record("model", index=1)),
        TrajectoryRecord.from_wire(wire_record("tool", index=2, lane="tools", kind="tool_call")),
    ]
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, records)
        panel = view.query_one(FilterPanel)
        view.action_toggle_filters()
        assert any(
            dimension is FilterDimension.LANE and value == "tools"
            for dimension, value in panel.options
        )
        assert "tools (1)" in panel.render().plain
        view.on_filter_value_clicked(
            type("Filter", (), {"dimension": FilterDimension.LANE, "value": "tools"})()
        )
        assert view.search_result.record_ids == ("tool",)


def test_duration_mode_changes_render_without_reordering() -> None:
    item = TrajectoryRecord.from_wire(
        wire_record(
            "r1",
            summary="[literal] \\ data",
        )
        | {"timing": {"duration_ms": 1250, "provenance": "exact"}}
    )
    order = record_line(item, 1, duration_mode=False).plain
    duration = record_line(item, 1, duration_mode=True).plain
    assert order != duration
    assert "dur" in duration
    assert sanitize_text("[literal] \\ data") == "[literal] \\ data"


async def test_clicks_and_movement_pause_tail_but_hover_does_not() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [record("r1"), record("r2", index=2, turn_id=None)])
        view.on_timeline_span_clicked(TimelineSpanClicked("r1"))
        assert not view.state.follow_tail
        view.state.resume_follow()
        view.on_ledger_record_hovered(type("Hover", (), {"record_id": "r1"})())
        assert view.state.follow_tail
        view.on_ledger_record_clicked(type("Click", (), {"record_id": "r1"})())
        assert not view.state.follow_tail


async def test_inspector_context_precedence_and_actionable_link_lines() -> None:
    item = TrajectoryRecord.from_wire(
        wire_record("system", kind="system", lane="model")
        | {
            "links": [{"participant_id": "other", "direction": "to"}],
            "details": {"payload": "value"},
        }
    )
    assert tabs_for_record(item) == (InspectorTab.CURRENT, InspectorTab.PREVIOUS, InspectorTab.DIFF)
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = await populate(app, [item])
        inspector = view.query_one(Inspector)
        assert inspector._link_line_ids
        assert "\\[" not in inspector.copy_text
        assert "other" in inspector_content(item, InspectorTab.CURRENT).plain


def test_oversized_cursor_batches_are_rejected() -> None:
    records = [wire_record(str(index), summary="x" * 16_000) for index in range(70)]
    with pytest.raises(WireDecodeError):
        TrajectoryPage.from_wire({"participant_id": "p1", "cursor": "c1", "records": records})
    with pytest.raises(WireDecodeError):
        TrajectoryFollow.from_wire({"participant_id": "p1", "cursor": "c2", "upserts": records})


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        if method == "trajectory.snapshot":
            return page(str(params["id"]), f"{params['id']}-record")
        return {"participant_id": str(params["id"]), "upserts": []}

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_controller_sends_best_effort_close_hints_on_switch_lru_and_final_close() -> None:
    query = FakeClient()
    follow = FakeClient()
    controller = TrajectoryController(query, follow)
    await controller.open("a", start_follow=False)
    await controller.open("b", start_follow=False)
    await asyncio.sleep(0)
    await controller.close()

    closed_ids = [params["id"] for method, params in query.calls if method == "trajectory.close"]
    assert "a" in closed_ids and "b" in closed_ids
    assert query.closed and follow.closed
