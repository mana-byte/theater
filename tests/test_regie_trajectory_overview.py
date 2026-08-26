from __future__ import annotations

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.containers import HorizontalScroll
from textual.widgets import Input, Label

from theater.constants.regie_trajectory import TRAJECTORY_OVERVIEW_HEIGHT
from theater.regie.trajectory.state import ParticipantTrajectoryState
from theater.regie.trajectory.view import TrajectoryView
from theater.regie.trajectory.widgets import overview as trajectory_overview
from theater.regie.trajectory.widgets.overview import TrajectoryOverviewStrip
from theater.regie.trajectory.widgets.timeline import Timeline
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryCapabilities,
    TrajectoryCurrentOperation,
    TrajectoryDelta,
    TrajectoryFeature,
    TrajectoryIncompleteReason,
    TrajectoryOverview,
    TrajectoryPage,
    TrajectoryProblem,
)
from theater.trajectory.overview import TrajectoryErrorDiagnostics, TrajectorySlowOperation


def panel(
    state: PanelState = PanelState.READY, participant_state: str = "live", message: str = ""
) -> PanelStateInfo:
    return PanelStateInfo(state, message, participant_state=participant_state)


def current(
    *,
    start: float | None = None,
    duration_ms: float | None = 8_400,
    summary: str = "pytest tests/test_rpc.py",
) -> TrajectoryCurrentOperation:
    return TrajectoryCurrentOperation(
        record_id="r1",
        kind="tool_call",
        lane="tools",
        status="running",
        summary=summary,
        model="gpt-5.6",
        start=start,
        duration_ms=duration_ms,
    )


def text(label: Label) -> str:
    return str(label.render())


class Host(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1", id="trajectory", focus_on_mount=False)


def test_state_snapshot_older_and_follow_copy_backend_overview_facts() -> None:
    state = ParticipantTrajectoryState("p1")
    first_capabilities = TrajectoryCapabilities(supported=frozenset({TrajectoryFeature.TOOLS}))
    first_overview = TrajectoryOverview(record_count=2)
    state.apply_snapshot(
        TrajectoryPage(
            panel_state=panel(),
            stream_id="stream",
            capabilities=first_capabilities,
            overview=first_overview,
        )
    )
    assert state.capabilities is first_capabilities
    assert state.overview is first_overview

    older_capabilities = TrajectoryCapabilities(unsupported=frozenset({TrajectoryFeature.USAGE}))
    older_overview = TrajectoryOverview(record_count=4)
    state.apply_older(
        TrajectoryPage(
            panel_state=panel(), capabilities=older_capabilities, overview=older_overview
        )
    )
    assert state.capabilities is older_capabilities
    assert state.overview is older_overview

    follow_capabilities = TrajectoryCapabilities(observed=frozenset({TrajectoryFeature.TIMING}))
    follow_overview = TrajectoryOverview(record_count=5)
    state.apply_follow(
        TrajectoryDelta(
            stream_id="stream", capabilities=follow_capabilities, overview=follow_overview
        )
    )
    assert state.capabilities is follow_capabilities
    assert state.overview is follow_overview


def test_legacy_follow_preserves_backend_overview_facts() -> None:
    capabilities = TrajectoryCapabilities(supported=frozenset({TrajectoryFeature.MODELS}))
    overview = TrajectoryOverview(record_count=3)
    state = ParticipantTrajectoryState(
        "p1", stream_id="stream", capabilities=capabilities, overview=overview
    )

    state.apply_follow(TrajectoryDelta(stream_id="stream"))

    assert state.capabilities is capabilities
    assert state.overview is overview


async def test_strip_mounts_between_search_and_timeline_at_fixed_height() -> None:
    app = Host()
    async with app.run_test(size=(100, 30)):
        view = app.query_one(TrajectoryView)
        search = view.query_one("#trajectory-search", Input)
        strip = view.query_one("#trajectory-overview", TrajectoryOverviewStrip)
        timeline = view.query_one(Timeline)
        assert search.region.y < strip.region.y < timeline.region.y
        assert strip.region.height == TRAJECTORY_OVERVIEW_HEIGHT
        assert not strip.can_focus


async def test_meta_line_scrolls_horizontally_without_visible_scrollbars() -> None:
    app = App()
    async with app.run_test(size=(32, 8)) as pilot:
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        strip.update_state(
            panel=panel(),
            capabilities=TrajectoryCapabilities(
                supported=frozenset(TrajectoryFeature),
                observed=frozenset(TrajectoryFeature),
            ),
            overview=TrajectoryOverview(
                record_count=1_500,
                model_operations=200,
                tool_operations=300,
                input_tokens=4_000,
                output_tokens=5_000,
            ),
            loading=False,
        )
        await pilot.pause()

        scroll = strip.query_one("#trajectory-overview-meta-scroll", HorizontalScroll)
        assert scroll.virtual_size.width > scroll.scrollable_content_region.width
        assert scroll.styles.scrollbar_size_horizontal == 0
        assert scroll.styles.scrollbar_size_vertical == 0
        scroll.post_message(
            events.MouseScrollDown(
                scroll,
                x=1,
                y=0,
                delta_x=0,
                delta_y=1,
                button=0,
                shift=False,
                meta=False,
                ctrl=False,
            )
        )
        await pilot.pause()
        assert scroll.scroll_x > 0
        scroll.scroll_to(x=scroll.max_scroll_x, animate=False, immediate=True)
        assert scroll.scroll_x == scroll.max_scroll_x > 0


async def test_live_current_renders_glyph_status_model_duration_and_summary() -> None:
    app = App()
    async with app.run_test() as pilot:
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        strip.update_state(
            panel=panel(),
            capabilities=TrajectoryCapabilities(),
            overview=TrajectoryOverview(current=current()),
            loading=False,
        )
        primary = text(strip.query_one("#trajectory-overview-current", Label))
        assert primary == "⚙ Running tool call · gpt-5.6 · 8.4s · pytest tests/test_rpc.py"
        await pilot.pause()


async def test_current_summary_markup_is_literal() -> None:
    app = App()
    async with app.run_test():
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        strip.update_state(
            panel=panel(),
            capabilities=TrajectoryCapabilities(),
            overview=TrajectoryOverview(current=current(summary="[red]oops[/]")),
            loading=False,
        )
        assert "[red]oops[/]" in text(strip.query_one("#trajectory-overview-current", Label))


@pytest.mark.parametrize("participant_state", ("unknown", "missing"))
async def test_unknown_and_missing_current_never_receive_active_styling(
    participant_state: str,
) -> None:
    app = App()
    async with app.run_test():
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        overview = TrajectoryOverview(current=current())
        strip.update_state(
            panel=panel(participant_state=participant_state),
            capabilities=TrajectoryCapabilities(),
            overview=overview,
            loading=False,
        )
        label = strip.query_one("#trajectory-overview-current", Label)
        assert not label.has_class("-active")
        assert "Running" not in text(label)
        strip.update_state(
            panel=panel(),
            capabilities=TrajectoryCapabilities(),
            overview=overview,
            loading=False,
        )
        assert label.has_class("-active")


async def test_dead_external_and_bad_panels_never_present_current_as_running() -> None:
    app = App()
    async with app.run_test():
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        overview = TrajectoryOverview(current=current())
        for state, participant_state in (
            (PanelState.READY, "dead"),
            (PanelState.READY, "external"),
            (PanelState.STALE, "live"),
            (PanelState.UNTRUSTED, "live"),
        ):
            strip.update_state(
                panel=panel(state, participant_state, "not live"),
                capabilities=TrajectoryCapabilities(),
                overview=overview,
                loading=False,
            )
            primary = text(strip.query_one("#trajectory-overview-current", Label))
            assert "Running" not in primary


async def test_idle_problem_and_meta_coverage_capabilities_and_totals() -> None:
    app = App()
    async with app.run_test():
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        capabilities = TrajectoryCapabilities(
            supported=frozenset({TrajectoryFeature.MODELS, TrajectoryFeature.TOOLS}),
            unsupported=frozenset({TrajectoryFeature.REQUESTS}),
            observed=frozenset({TrajectoryFeature.MODELS, TrajectoryFeature.CONTEXT}),
        )
        overview = TrajectoryOverview(
            incomplete_reasons=(
                TrajectoryIncompleteReason.OLDER_HISTORY,
                TrajectoryIncompleteReason.COVERAGE_GAPS,
                TrajectoryIncompleteReason.CACHE_EVICTED,
            ),
            record_count=1_500,
            model_operations=2,
            tool_operations=3,
            input_tokens=1_200,
            output_tokens=4,
            cache_read_tokens=5,
            cache_write_tokens=6,
            reasoning_tokens=7,
            reported_cost_usd=0.0034,
            totals_saturated=True,
            latest_problem=TrajectoryProblem(record_id="r2", summary="last failure"),
        )
        strip.update_state(
            panel=panel(), capabilities=capabilities, overview=overview, loading=False
        )
        primary = text(strip.query_one("#trajectory-overview-current", Label))
        meta = strip.query_one("#trajectory-overview-meta", Label)
        assert primary == "Idle · no active operation · last issue: last failure"
        assert "1.5K cached records" in text(meta)
        assert "in 1.2K tok" in text(meta)
        assert "reported $0.0034" in text(meta)
        assert "totals capped" in text(meta)
        assert "partial: older history, gaps, cache eviction" in text(meta)
        assert "2 supported · 2 observed · 1 unsupported" in text(meta)
        assert meta.tooltip == (
            "Coverage: older_history, coverage_gaps, cache_evicted\n"
            "Supported: models, tools\n"
            "Unsupported: requests\n"
            "Observed: models, context\n"
            "Unknown: usage, timing, reasoning, context, retries, live_updates"
        )


async def test_complete_and_unknown_coverage_wording() -> None:
    app = App()
    async with app.run_test():
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        for reasons, wording in (
            ((), "coverage complete"),
            ((TrajectoryIncompleteReason.UNKNOWN,), "coverage unknown"),
        ):
            strip.update_state(
                panel=panel(),
                capabilities=TrajectoryCapabilities(),
                overview=TrajectoryOverview(incomplete_reasons=reasons),
                loading=False,
            )
            assert wording in text(strip.query_one("#trajectory-overview-meta", Label))


async def test_meta_renders_cost_provenance_duration_errors_retries_and_slowest_operations() -> (
    None
):
    app = App()
    async with app.run_test():
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        overview = TrajectoryOverview(
            incomplete_reasons=(),
            reported_cost_usd=0.1,
            estimated_cost_usd=0.2,
            unknown_cost_usd=0.3,
            active_duration_ms=2_000,
            diagnostics=TrajectoryErrorDiagnostics(error_count=2, retry_count=1),
            slowest_model_operation=TrajectorySlowOperation(
                "model-record",
                "request",
                "model-x",
                1_500,
                "completed",
            ),
            slowest_tool_operation=TrajectorySlowOperation(
                "tool-record",
                "tool",
                "pytest",
                750,
                "completed",
            ),
        )
        strip.update_state(
            panel=panel(),
            capabilities=TrajectoryCapabilities(),
            overview=overview,
            loading=False,
        )

        meta = strip.query_one("#trajectory-overview-meta", Label)
        value = text(meta)
        assert "reported $0.1" in value
        assert "estimated $0.2" in value
        assert "unclassified $0.3" in value
        assert "active 2.0s" in value
        assert "2 errors" in value and "1 retries" in value
        assert "Slowest model: model-x · 1.5s" in str(meta.tooltip)
        assert "Slowest tool: pytest · 750ms" in str(meta.tooltip)


async def test_tick_updates_only_elapsed_primary_and_identical_state_is_quiet(monkeypatch) -> None:
    app = App()
    async with app.run_test():
        strip = TrajectoryOverviewStrip()
        await app.mount(strip)
        monkeypatch.setattr(trajectory_overview.time, "time", lambda: 100.0)
        overview = TrajectoryOverview(current=current(start=98.0, duration_ms=None))
        strip.update_state(
            panel=panel(), capabilities=TrajectoryCapabilities(), overview=overview, loading=False
        )
        primary = strip.query_one("#trajectory-overview-current", Label)
        assert "2.0s" in text(primary)
        calls = 0
        original_update = primary.update

        def count_update(value: object = "") -> None:
            nonlocal calls
            calls += 1
            original_update(value)

        monkeypatch.setattr(primary, "update", count_update)
        strip.update_state(
            panel=panel(), capabilities=TrajectoryCapabilities(), overview=overview, loading=False
        )
        assert calls == 0
        monkeypatch.setattr(trajectory_overview.time, "time", lambda: 101.0)
        strip._tick()
        assert calls == 1
        assert "3.0s" in text(primary)
