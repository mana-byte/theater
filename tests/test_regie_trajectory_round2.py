from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from textual.app import App, ComposeResult

from theater.regie.trajectory.constants import FILTER_MAX_ROWS, LEDGER_OVERSCAN_ROWS
from theater.regie.trajectory.controller import TrajectoryController
from theater.regie.trajectory.filter_panel import FilterPanel
from theater.regie.trajectory.inspector import (
    Inspector,
    InspectorParticipantLinkClicked,
    InspectorResizeRequested,
)
from theater.regie.trajectory.ledger import Ledger
from theater.regie.trajectory.models import (
    InspectorTab,
    Lane,
    TrajectoryRecord,
)
from theater.regie.trajectory.render import inspector_content, inspector_text
from theater.regie.trajectory.search import FilterCounts, search_records
from theater.regie.trajectory.timeline import Timeline
from theater.regie.trajectory.view import (
    TrajectoryParticipantSelected,
    TrajectoryView,
)


def wire_record(
    record_id: str,
    *,
    participant_id: str = "p1",
    kind: str = "assistant",
    lane: str = "model",
    summary: str = "summary",
    details: dict[str, object] | None = None,
    links: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "record_id": record_id,
        "revision": 1,
        "participant_id": participant_id,
        "lane": lane,
        "kind": kind,
        "source": "claude",
        "summary": summary,
        "status": "completed",
        "details": details or {},
        "links": links or [],
    }


def record(record_id: str, **overrides: object) -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(wire_record(record_id, **overrides))


def page(participant_id: str, record_id: str, *, older: bool = False) -> dict[str, object]:
    return {
        "participant_id": participant_id,
        "panel": "ready",
        "stream_id": f"stream-{participant_id}",
        "cursor": "cursor",
        "older_cursor": "older" if older else None,
        "has_older": older,
        "records": [wire_record(record_id, participant_id=participant_id)],
    }


class FakeClient:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.closed = False

    async def call(self, method: str, **params: object) -> object:
        result = self.handler(method, params)
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_older_loading_clears_after_stale_generation_exception() -> None:
    release = asyncio.Event()

    async def query_handler(_method: str, params: dict[str, object]) -> object:
        if params["id"] == "a" and params.get("before") == "older":
            await release.wait()
            raise RuntimeError("late page")
        return page(str(params["id"]), "record", older=params["id"] == "a")

    query = FakeClient(query_handler)
    follow = FakeClient(lambda _method, _params: {"participant_id": "b", "upserts": []})
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


class ControllerHost(App):
    def __init__(self, controller: TrajectoryController) -> None:
        super().__init__()
        self.controller = controller

    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1", controller=self.controller)


@pytest.mark.asyncio
async def test_view_unmount_does_not_close_shared_controller() -> None:
    query = FakeClient(
        lambda _method, params: page(str(params["id"]), "record") | {"panel": "dead"}
    )
    follow = FakeClient(
        lambda _method, params: {"participant_id": str(params["id"]), "upserts": []}
    )
    controller = TrajectoryController(query, follow)
    app = ControllerHost(controller)
    async with app.run_test(size=(80, 20)):
        view = app.query_one(TrajectoryView)
        await asyncio.sleep(0)
        view.on_unmount()
        assert not query.closed
        assert not follow.closed
    assert not query.closed
    assert not follow.closed
    await controller.close()


class InspectorHost(App):
    def __init__(self, item: TrajectoryRecord) -> None:
        super().__init__()
        self.item = item
        self.links: list[str] = []
        self.resize: list[float] = []

    def compose(self) -> ComposeResult:
        yield Inspector(self.item)

    def on_inspector_participant_link_clicked(
        self, message: InspectorParticipantLinkClicked
    ) -> None:
        self.links.append(message.participant_id)

    def on_inspector_resize_requested(self, message: InspectorResizeRequested) -> None:
        self.resize.append(message.delta)


@pytest.mark.asyncio
async def test_mounted_inspector_maximize_survives_refresh_ratio_and_drag() -> None:
    item = record("r1")
    app = InspectorHost(item)
    async with app.run_test(size=(80, 20)) as pilot:
        inspector = app.query_one(Inspector)
        inspector.set_ratio(0.60)
        inspector.toggle_maximize()
        inspector.set_ratio(0.25)
        inspector.set_record(item, tab=InspectorTab.SUMMARY)
        inspector.resize_by(0.10)
        assert inspector.maximized
        assert str(inspector.styles.height) == "1fr"
        inspector._resizing = True
        inspector.on_mouse_move(SimpleNamespace(delta_y=2, stop=lambda: None))
        await pilot.pause()
        assert app.resize and app.resize[-1] < 0
        inspector.toggle_maximize()
        assert not inspector.maximized
        assert str(inspector.styles.height) == "35h"


@pytest.mark.asyncio
async def test_filter_cursor_is_styled_and_scrolled_into_view() -> None:
    class FilterHost(App):
        def compose(self) -> ComposeResult:
            yield FilterPanel()

    app = FilterHost()
    async with app.run_test(size=(80, FILTER_MAX_ROWS // 2 + 2)) as pilot:
        panel = app.query_one(FilterPanel)
        panel.update_filters(
            FilterCounts(
                lanes=dict.fromkeys(Lane, 1),
                kinds={},
                statuses={},
                sources={f"source-{index}": 1 for index in range(20)},
            ),
            lanes=set(),
            kinds=set(),
            statuses=set(),
            sources=set(),
        )
        panel.focus()
        await pilot.press(*(["j"] * (len(panel.options) - 1)))
        assert panel._cursor == len(panel.options) - 1
        assert panel._scroll_offset > 0
        assert panel._scroll_offset <= panel._cursor < panel._scroll_offset + FILTER_MAX_ROWS
        assert any(span.style and span.style.reverse for span in panel.render().spans)


@pytest.mark.asyncio
async def test_links_use_exact_lines_and_callback_excludes_fallback_message() -> None:
    item = record(
        "system",
        kind="system",
        links=[
            {"participant_id": "p", "direction": "to"},
            {"participant_id": "p-long", "direction": "to"},
        ],
    )
    app = InspectorHost(item)
    async with app.run_test(size=(80, 20)) as pilot:
        inspector = app.query_one(Inspector)
        short_line = next(line for line, value in inspector._link_line_ids.items() if value == "p")
        inspector.on_click(SimpleNamespace(y=short_line, stop=lambda: None))
        await pilot.pause()
        assert app.links == ["p"]

    called: list[str] = []

    class LinkViewHost(App):
        def compose(self) -> ComposeResult:
            yield TrajectoryView("p1", participant_link=called.append)

        def on_trajectory_participant_selected(
            self, _message: TrajectoryParticipantSelected
        ) -> None:
            called.append("fallback")

    link_app = LinkViewHost()
    async with link_app.run_test(size=(80, 20)) as pilot:
        view = link_app.query_one(TrajectoryView)
        view.on_inspector_participant_link_clicked(InspectorParticipantLinkClicked("p"))
        await pilot.pause()
    assert called == ["p"]


@pytest.mark.asyncio
async def test_offsets_stop_at_the_last_full_viewport_start() -> None:
    records = [record(f"r{index}", summary=str(index)) for index in range(20)]

    class OffsetHost(App):
        def compose(self) -> ComposeResult:
            yield Ledger()
            yield Timeline()

    app = OffsetHost()
    async with app.run_test(size=(80, 20)):
        ledger = app.query_one(Ledger)
        ledger._viewport_height = 5
        ledger.update_rows(records, search_records(records))
        ledger.set_scroll_offset(10_000)
        assert ledger._scroll_offset == len(ledger.entries) - ledger.viewport_rows
        assert len(ledger.rendered_line_ids) <= ledger.viewport_rows + 2 * LEDGER_OVERSCAN_ROWS

        timeline = app.query_one(Timeline)
        timeline._viewport_width = 8
        timeline.update_records(records)
        timeline.set_scroll_offset(10_000)
        assert timeline.horizontal_offset == len(records) - timeline._available_cells()


@pytest.mark.asyncio
async def test_context_tabs_render_matching_bounded_formats_and_copy_exactly() -> None:
    item = record(
        "system",
        kind="system",
        details={
            "current": {"format": "json", "text": '{"z": 1, "a": [2]}'},
            "previous": {"format": "markdown", "text": "[old] \\ path"},
            "diff": {"format": "diff", "text": "--- old\n+++ new\n@@ -1 +1 @@"},
        },
    )
    current = inspector_text(item, InspectorTab.CURRENT)
    previous = inspector_text(item, InspectorTab.PREVIOUS)
    diff = inspector_text(item, InspectorTab.DIFF)
    assert '"a": [' in current and "No current" not in current
    assert "[old] \\ path" in previous and "No previous" not in previous
    assert "--- old" in diff and "No context diff" not in diff

    class ContextHost(App):
        def compose(self) -> ComposeResult:
            yield Inspector(item)

    app = ContextHost()
    async with app.run_test(size=(80, 20)):
        inspector = app.query_one(Inspector)
        inspector.set_tab(InspectorTab.CURRENT)
        assert inspector.copy_text == inspector_content(item, InspectorTab.CURRENT).plain
