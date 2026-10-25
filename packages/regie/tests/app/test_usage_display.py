"""Usage in the UI: footer toggles, per-agent cost display and refresh failures."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import cast

import pytest
from regie.state import StateController
from regie.widgets import ParticipantTree, UsageBreakdownPanel, UsageMetricTile
from regie.widgets.leaf import AgentLeaf

from packages.regie.tests.support.app_fakes import (
    FakeState,
    build_app,
    make_projection,
)
from packages.regie.tests.support.waiting import press_until, wait_until
from theater.frontend import (
    FrontendTransportError,
    StateProjection,
)


@pytest.mark.asyncio
async def test_usage_footer_keyboard_pointer_and_detailed_mode_share_state() -> None:
    app, client, _presentation = build_app()

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        tree = app.query_one(ParticipantTree)
        panel = app.query_one(UsageBreakdownPanel)
        await press_until(
            pilot, "j", lambda: app._usage_panel.in_footer
        )  # past the menu closing the tree
        assert app._usage_panel.keyboard_metric == "input"
        assert panel.has_class("-visible")
        assert not list(tree.query(".tree-cursor"))
        await pilot.press("x")
        assert client.participants.terminated == []

        await pilot.press("right")
        assert app._usage_panel.active_metric == "output"
        cache = app.query_one("#cache-col", UsageMetricTile)
        await pilot.hover(cache)
        await pilot.pause()
        assert app._usage_panel.active_metric == "cache"

        await pilot.click(cache)
        await pilot.pause()
        assert app._usage_panel.detailed
        assert any(call["detailed"] is True for call in client.usage.by_harness_calls)

        await pilot.hover(tree)
        await pilot.pause()
        assert app._usage_panel.active_metric == "output"
        await pilot.press("up")
        assert not app._usage_panel.in_footer
        assert not panel.has_class("-visible")
        assert list(tree.query(".tree-cursor"))


@pytest.mark.asyncio
async def test_usage_footer_starts_hidden_and_dollar_toggles_only_the_footer() -> None:
    app, _client, _presentation = build_app(usage_visible=False)
    footers = ("#usage-period", "#stats-footer", "#price-footer")

    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause()
        assert not any(app.query_one(footer).display for footer in footers)
        # With the footer hidden, moving past the last row must not enter it.
        await pilot.press("j", "j", "j")
        assert not app._usage_panel.in_footer

        await pilot.press("dollar_sign")
        assert all(app.query_one(footer).display for footer in footers)
        await press_until(pilot, "j", lambda: app._usage_panel.in_footer)

        await pilot.press("dollar_sign")
        assert not any(app.query_one(footer).display for footer in footers)
        assert not app._usage_panel.in_footer
        assert not app.query_one(UsageBreakdownPanel).has_class("-visible")


@pytest.mark.asyncio
async def test_agent_cost_shows_on_the_selected_row_only_even_with_the_footer_hidden(
    fast_count_up,
) -> None:
    app, client, _presentation = build_app(usage_visible=False)

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)

        def row(participant_id: str) -> str:
            leaf = tree._key_widgets.get(("p", participant_id))
            return str(leaf.render()) if isinstance(leaf, AgentLeaf) else ""

        await wait_until(pilot, lambda: tree.selected_participant_id is not None, 5.0)
        if tree.selected_participant_id != "participant-1":
            await pilot.press("k")
        await wait_until(pilot, lambda: "$0.42" in row("participant-1"), 5.0)
        assert client.usage.by_participant_calls[0]["participant_ids"] == (
            "participant-1",
            "participant-2",
        )

        await pilot.press("j")
        await wait_until(pilot, lambda: tree.selected_participant_id == "participant-2", 5.0)
        assert "$0.42" not in row("participant-1")


class _LateState(FakeState):
    """Like the real controller: no projection until the initial snapshot lands."""

    def __init__(self, projection: StateProjection) -> None:
        super().__init__(projection)
        self._loaded = projection
        self.projection = None

    async def initialize(self) -> StateProjection:
        await asyncio.sleep(0.2)
        self.projection = self._loaded
        return await super().initialize()


@pytest.mark.asyncio
async def test_startup_usage_waits_for_participants_so_costs_appear_immediately(
    fast_count_up,
) -> None:
    app, client, _presentation = build_app(usage_visible=False)
    app._state = cast(StateController, _LateState(make_projection()))

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)

        def row() -> str:
            leaf = tree._key_widgets.get(("p", "participant-1"))
            return str(leaf.render()) if isinstance(leaf, AgentLeaf) else ""

        # Well inside the 10 s poll: the first usage read must already carry the agents.
        await wait_until(pilot, lambda: "$0.42" in row(), 4.0)
        assert client.usage.by_participant_calls[0]["participant_ids"] == (
            "participant-1",
            "participant-2",
        )


@pytest.mark.asyncio
async def test_hovering_an_unselected_agent_shows_its_cost_until_the_pointer_leaves(
    fast_count_up,
) -> None:
    app, _client, _presentation = build_app(usage_visible=False)

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)

        def leaf(participant_id: str) -> AgentLeaf | None:
            widget = tree._key_widgets.get(("p", participant_id))
            return widget if isinstance(widget, AgentLeaf) else None

        def row(participant_id: str) -> str:
            widget = leaf(participant_id)
            return str(widget.render()) if widget is not None else ""

        await wait_until(pilot, lambda: "$0.42" in row("participant-1"), 5.0)
        await pilot.press("j")
        await wait_until(pilot, lambda: tree.selected_participant_id == "participant-2", 5.0)
        assert "$0.42" not in row("participant-1")

        await pilot.hover(leaf("participant-1"))
        await wait_until(pilot, lambda: "$0.42" in row("participant-1"), 5.0)

        await pilot.hover(leaf("participant-2"))
        await wait_until(pilot, lambda: "$0.42" not in row("participant-1"), 5.0)


@pytest.mark.asyncio
async def test_selected_agent_cost_counts_up_like_the_footer_and_others_just_update(
    fast_count_up, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, client, _presentation = build_app(usage_visible=False)
    ticks: list[str] = []  # every frame the selected agent's count-up drew, in order
    count_tick = AgentLeaf._tick_cost

    def recording_tick(leaf: AgentLeaf) -> None:
        count_tick(leaf)
        if leaf.participant_id == "participant-1":
            ticks.append(str(leaf.render()))

    monkeypatch.setattr(AgentLeaf, "_tick_cost", recording_tick)

    async with app.run_test(size=(100, 36)) as pilot:
        tree = app.query_one(ParticipantTree)

        def row(participant_id: str) -> str:
            leaf = tree._key_widgets.get(("p", participant_id))
            return str(leaf.render()) if isinstance(leaf, AgentLeaf) else ""

        await wait_until(pilot, lambda: tree.selected_participant_id is not None, 5.0)
        assert tree.selected_participant_id == "participant-1"
        # Startup: the first cost counts up from zero, as the footer does.
        await wait_until(pilot, lambda: "$0.42" in row("participant-1"), 5.0)
        assert any("$0." in tick and "$0.42" not in tick for tick in ticks)
        ticks.clear()

        # Selected: the count passes through intermediate values before settling.
        client.usage.participant_cost_microcents = 142_000_000
        await app._refresh_usage()
        await wait_until(pilot, lambda: "$1.42" in row("participant-1"), 5.0)
        assert any("$0.42" not in tick and "$1.42" not in tick for tick in ticks)

        # Unselected: the value is adopted silently and shows as is on selection.
        await pilot.press("j")
        client.usage.participant_cost_microcents = 242_000_000
        await app._refresh_usage()
        await pilot.press("k")
        await wait_until(pilot, lambda: tree.selected_participant_id == "participant-1", 5.0)
        assert "$2.42" in row("participant-1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [FrontendTransportError("footer poll failed"), TypeError("malformed usage")],
)
async def test_usage_footer_poll_failure_does_not_contaminate_overlay_cache(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    app, _client, _presentation = build_app()

    async with app.run_test() as pilot:
        await pilot.pause()
        app._usage_panel.active_metric = "input"
        app._usage_panel.breakdown = {"harnesses": [{"harness": "codex"}]}
        app._usage_panel.message = None

        async def fail_refresh(*, window: str, participant_ids: tuple[str, ...]) -> object:
            del window, participant_ids
            raise error

        monkeypatch.setattr(app._usage, "refresh", fail_refresh)
        await app._refresh_usage()

        assert app._usage_panel.breakdown == {"harnesses": [{"harness": "codex"}]}
        assert app._usage_panel.message is None


@pytest.mark.asyncio
async def test_configured_usage_period_survives_an_initial_refresh_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client, _presentation = build_app()
    app.settings = replace(app.settings, cost_window="month")

    async def fail_refresh(*, window: str, participant_ids: tuple[str, ...]) -> object:
        assert window == "month"
        del participant_ids
        raise FrontendTransportError("usage unavailable")

    monkeypatch.setattr(app._usage, "refresh", fail_refresh)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert str(app.query_one("#usage-period").render()) == "this month"
