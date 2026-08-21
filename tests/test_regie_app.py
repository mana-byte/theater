"""The régie, driven for real through Textual's test pilot.

Everything else about the régie is tested against pure functions — rendering in
test_regie_tree, bus lines in test_regie_bus, teardown in test_regie_teardown.
What those cannot see is the app itself: whether mount wires the panels up,
whether a keypress reaches the action it is bound to, and whether the tmux
calls that stage a pane happen in the right order with the right arguments.

The two boundaries are faked and nothing else is: `DaemonClient` becomes a
recorder with canned answers, and the tmux module functions become recorders.
The widgets, the reactives, the bindings and the render path are the real ones.
"""

from __future__ import annotations

import asyncio
import inspect
from io import StringIO

import pytest
from rich.cells import cell_len
from rich.console import Console
from rich.table import Table
from textual.color import Color
from textual.selection import SELECT_ALL
from textual.theme import BUILTIN_THEMES

from theater.config import Config, RegieSection
from theater.constants import (
    MICROCENTS_PER_DOLLAR,
    USAGE_AVERAGE_WINDOW_DAYS,
    USAGE_AVERAGE_WINDOW_HOURS,
)
from theater.protocol import RemoteError
from theater.regie import app as app_mod
from theater.regie.app import RegieApp
from theater.regie.tree import SEND_STYLE, send_path

PARENT = {
    "id": "aaaaaaaaaaaa",
    "tier": "spawned",
    "harness": "vibe",
    "status": "idle",
    "cwd": "/tmp/proj",
    "tmux_pane": "%10",
    "addressable": True,
    "children": [],
}

CHILD = {
    "id": "bbbbbbbbbbbb",
    "tier": "spawned",
    "harness": "claude",
    "status": "working",
    "cwd": "/tmp/proj/child",
    "tmux_pane": "%11",
    "addressable": True,
    "children": [],
}

BUS_ROW = {
    "id": 1,
    "ts": 1723000000,
    "kind": "agent.assistant",
    "from_id": "aaaaaaaaaaaa",
    "to_id": None,
    "payload": {"text": "hello", "tool": None, "ts": None, "turn_end": True, "index": 0},
}

SEND_ROW = {
    "id": 1,
    "ts": 1723000000,
    "kind": "agent.send",
    "from_id": PARENT["id"],
    "to_id": CHILD["id"],
    "payload": {"handle": "h1", "prompt": "do the thing"},
}

SPAWN_ROW = {
    "id": 1,
    "ts": 1723000000,
    "kind": "participant.created",
    "from_id": PARENT["id"],
    "to_id": CHILD["id"],
    "payload": {
        "tier": "spawned",
        "harness": CHILD["harness"],
        "cwd": CHILD["cwd"],
        "has_prompt": True,
    },
}

AWAIT_START_ROW = {
    "id": 1,
    "ts": 1723000000,
    "kind": "job.await.start",
    "from_id": PARENT["id"],
    "to_id": CHILD["id"],
    "payload": {"handle": CHILD["id"], "token": "await-token"},
}

AWAIT_END_ROW = {
    **AWAIT_START_ROW,
    "id": 2,
    "kind": "job.await.end",
}

_ECHO_PERIOD = object()


class FakeClient:
    """A DaemonClient that answers from a dict and remembers what was asked."""

    def __init__(self, answers: dict, broken: set[str]):
        self.answers = answers
        self.broken = broken
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def call(self, method: str, **params):
        self.calls.append((method, params))
        if method in self.broken:
            raise RuntimeError(f"{method} is unavailable")
        answer = self.answers.get(method, [])
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(params)
        if inspect.isawaitable(answer):
            answer = await answer
        if (
            method == "usage_summary"
            and isinstance(answer, dict)
            and answer.get("period") is _ECHO_PERIOD
        ):
            return {**answer, "period": params.get("period")}
        return answer

    async def aclose(self) -> None:
        self.closed = True

    def asked(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def daemon(monkeypatch):
    """Install a fake DaemonClient and hand the test its recorder."""
    state: dict = {
        "answers": {
            "participants.tree": [dict(PARENT, children=[dict(CHILD)])],
            "participants.unmanaged": [],
            "bus.tail": [],
            "harnesses": [{"name": "vibe"}, {"name": "claude"}],
            "usage_summary": {
                "period": _ECHO_PERIOD,
                "all_time": {"input_tokens": 11, "output_tokens": 7},
                "windowed": {
                    "input_tokens": 5,
                    "output_tokens": 3,
                    "reasoning_output_tokens": 2,
                    "cache_read_input_tokens": 7,
                    "cache_creation_input_tokens": 11,
                    "cost_microcents": 25_000_000,
                },
                "average": {"cost_microcents": 300_000_000, "active_days": 3},
            },
            "usage_by_harness": {
                "harnesses": [
                    {
                        "harness": "vibe",
                        "today": {
                            "input_tokens": 1_000,
                            "output_tokens": 2_000,
                            "reasoning_output_tokens": 500,
                            "cache_read_input_tokens": 300,
                            "cache_creation_input_tokens": 200,
                            "cost_microcents": 100_000_000,
                            "active_days": 2,
                        },
                        "week": {
                            "input_tokens": 2_000,
                            "output_tokens": 4_000,
                            "reasoning_output_tokens": 1_000,
                            "cache_read_input_tokens": 600,
                            "cache_creation_input_tokens": 400,
                            "cost_microcents": 200_000_000,
                            "active_days": 4,
                        },
                        "month": {
                            "input_tokens": 3_000,
                            "output_tokens": 6_000,
                            "reasoning_output_tokens": 1_500,
                            "cache_read_input_tokens": 900,
                            "cache_creation_input_tokens": 600,
                            "cost_microcents": 300_000_000,
                            "active_days": 6,
                        },
                    },
                    {
                        "harness": "claude",
                        "today": {},
                        "week": {},
                        "month": {},
                    },
                ]
            },
        },
        "broken": set(),
        "client": None,
    }

    def factory(*_args, **_kwargs):
        client = FakeClient(state["answers"], state["broken"])
        state["client"] = client
        return client

    monkeypatch.setattr(app_mod, "DaemonClient", factory)
    return state


@pytest.fixture
def tmux(monkeypatch):
    """Fake the tmux surface the app touches; record the calls in order."""
    calls: list[tuple] = []

    async def display_message(fmt, *, target=None):
        return {
            "#{window_id}": "@7",
            "#{session_id}": "$2",
            "#{session_name}": "work",
        }[fmt]

    async def show_option(name, *, target):
        return None

    async def set_option(name, value, *, target):
        calls.append(("set", name, value))

    async def unset_option(name, *, target):
        calls.append(("unset", name))

    async def join_pane(pane, *, target_window=None):
        calls.append(("join", pane, target_window))

    async def break_pane(pane, *, target_window=None):
        calls.append(("break", pane))

    async def resize_pane(pane, *, width=None):
        calls.append(("resize", pane, width))

    async def select_pane(pane):
        calls.append(("select", pane))

    async def bind_key_if_free(table, key, command, *, note):
        calls.append(("bind", table, key, tuple(command)))
        return True

    async def unbind_key_if_owned(table, key, *, note):
        calls.append(("unbind", table, key))

    monkeypatch.setattr(app_mod.tmux, "current_pane", lambda: "%1")
    monkeypatch.setattr(app_mod.tmux, "display_message", display_message)
    monkeypatch.setattr(app_mod.tmux, "show_option", show_option)
    monkeypatch.setattr(app_mod.tmux, "set_option", set_option)
    monkeypatch.setattr(app_mod.tmux, "unset_option", unset_option)
    monkeypatch.setattr(app_mod.tmux, "bind_key_if_free", bind_key_if_free)
    monkeypatch.setattr(app_mod.tmux, "unbind_key_if_owned", unbind_key_if_owned)
    monkeypatch.setattr(app_mod.panes, "join_pane", join_pane)
    monkeypatch.setattr(app_mod.panes, "break_pane", break_pane)
    monkeypatch.setattr(app_mod.panes, "resize_pane", resize_pane)
    monkeypatch.setattr(app_mod.panes, "select_pane", select_pane)
    return calls


def make_app(**regie) -> tuple[RegieApp, list[tuple[str, str]]]:
    """An app with slow timers, and the list its notifications land in.

    The intervals are pushed out of the way so the only refreshes in a test are
    the ones at mount and the ones an action asks for. A one-second tree poll
    would otherwise race every assertion about what was called.
    """
    settings = Config(regie=RegieSection(tree_interval=60, bus_interval=60, **regie))
    app = RegieApp(settings)
    notes: list[tuple[str, str]] = []
    app.notify = lambda msg, **kw: notes.append(  # type: ignore[method-assign]
        (str(msg), kw.get("severity", "information"))
    )
    return app, notes


def _styles(widget) -> list[str]:
    return [span.style for span in widget.render().spans]


def _overlay_styles(widget) -> list[str]:
    styles = []
    for glyph in (widget._overlay or {}).values():
        styles.append(glyph[1] if isinstance(glyph, tuple) else glyph)
    return styles


def _overlay_glyphs(widget) -> list[str]:
    glyphs = []
    for glyph in (widget._overlay or {}).values():
        glyphs.append(glyph[0] if isinstance(glyph, tuple) else glyph)
    return glyphs


def _usage_breakdown_text(panel: app_mod.UsageBreakdownPanel) -> str:
    """Render all three pieces of the usage overlay as plain terminal text."""
    title = panel.query_one("#usage-breakdown-title", app_mod.NonSelectableStatic)
    body = panel.query_one("#usage-breakdown-content", app_mod.NonSelectableStatic)
    note = panel.query_one("#usage-breakdown-note", app_mod.NonSelectableStatic)
    if isinstance(body.content, Table):
        stream = StringIO()
        console = Console(
            file=stream,
            width=max(1, body.content_size.width),
            color_system=None,
            force_terminal=False,
        )
        console.print(body.content)
        body_text = stream.getvalue()
    else:
        body_text = str(body.render())
    return "\n".join((str(title.render()), body_text, str(note.render())))


def _color_delta(left: Color, right: Color) -> int:
    """Summed RGB distance, sufficient for theme-surface contrast assertions."""
    return sum(abs(a - b) for a, b in zip(left[:3], right[:3], strict=True))


# ---- mount ---------------------------------------------------------------


async def test_mount_fills_the_tree_and_the_bus(daemon, tmux):
    daemon["answers"]["bus.tail"] = [BUS_ROW]
    app, _ = make_app(bus_visible=True)
    async with app.run_test():
        assert len(app.tree_lines) == 2  # parent and child
        assert app.bus_cursor == 1
        assert app.harnesses == [{"name": "vibe"}, {"name": "claude"}]
        # The panel holds one Label per line, and knows which node each is.
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert len(panel.children) == 2
        assert panel._lines_data[1][1]["id"] == CHILD["id"]
        sidebar = app.query_one("#sidebar")
        assert [child.id for child in sidebar.children] == [
            "tree-stack",
            "usage-period",
            "stats-footer",
            "price-footer",
            "bus-panel",
        ]
        stack = app.query_one("#tree-stack")
        assert [child.id for child in stack.children] == ["tree-panel", "usage-breakdown"]


async def test_mount_fetches_all_usage_windows_with_one_rpc(daemon, tmux):
    app, _ = make_app(cost_window="week")
    async with app.run_test():
        client = daemon["client"]
        assert client.asked("usage_summary") == [{"window": 168.0, "period": "week"}]
        assert client.asked("usage_totals") == []
        stats = app.query_one("#stats-footer", app_mod.StatsFooter)
        period = app.query_one("#usage-period", app_mod.UsagePeriodBar)
        price = app.query_one("#price-footer", app_mod.PriceFooter)
        assert stats.totals == daemon["answers"]["usage_summary"]["windowed"]
        assert price.totals == daemon["answers"]["usage_summary"]["windowed"]
        assert price.daily_avg == 1.0
        assert period.period_label == "this week"


async def test_month_cost_window_uses_calendar_month(daemon, tmux):
    app, _ = make_app(cost_window="month")
    async with app.run_test():
        client = daemon["client"]
        assert client.asked("usage_summary") == [{"window": 720.0, "period": "month"}]
        assert app.query_one("#usage-period", app_mod.UsagePeriodBar).period_label == "this month"


async def test_unknown_cost_window_warns_once_and_uses_day(daemon, tmux):
    app, notes = make_app(cost_window="fortnight")
    async with app.run_test():
        client = daemon["client"]
        assert client.asked("usage_summary") == [{"window": 24.0, "period": "day"}]
        period = app.query_one("#usage-period", app_mod.UsagePeriodBar)
        assert period.period_label == "today"
        assert [message for message, severity in notes if severity == "warning"] == [
            "unknown cost_window 'fortnight' — using 'day'. available: day, month, week, year"
        ]


async def test_old_daemon_falls_back_to_two_usage_totals_calls(daemon, tmux):
    totals = {"input_tokens": 2, "output_tokens": 3, "cost_microcents": 90}
    daemon["answers"]["usage_summary"] = RemoteError("unknown_method", "old daemon")
    daemon["answers"]["usage_totals"] = totals
    app, _ = make_app(cost_window="year")

    async with app.run_test():
        client = daemon["client"]
        assert client.asked("usage_summary") == [{"window": 8760.0, "period": "year"}]
        assert client.asked("usage_totals") == [
            {"window": 8760.0},
            {"window": USAGE_AVERAGE_WINDOW_HOURS},
        ]
        period = app.query_one("#usage-period", app_mod.UsagePeriodBar)
        price = app.query_one("#price-footer", app_mod.PriceFooter)
        assert period.period_label == "last 365d"
        assert price.daily_avg == pytest.approx(
            90 / MICROCENTS_PER_DOLLAR / USAGE_AVERAGE_WINDOW_DAYS
        )


async def test_non_dict_usage_summary_logs_actual_type_and_skips_update(daemon, tmux, caplog):
    daemon["answers"]["usage_summary"] = [42]
    caplog.set_level("DEBUG", logger="theater.regie")
    app, _ = make_app()

    async with app.run_test():
        stats = app.query_one("#stats-footer", app_mod.StatsFooter)
        price = app.query_one("#price-footer", app_mod.PriceFooter)
        assert stats.totals is None
        assert price.totals is None

    assert "usage refresh returned list, expected dict" in caplog.text
    assert caplog.text.count("usage refresh returned") == 1


async def test_usage_summary_without_period_echo_uses_rolling_label(daemon, tmux):
    daemon["answers"]["usage_summary"].pop("period")
    app, _ = make_app(cost_window="week")

    async with app.run_test():
        period = app.query_one("#usage-period", app_mod.UsagePeriodBar)
        assert period.period_label == "last 7d"


async def test_usage_summary_with_unknown_period_echo_uses_rolling_label(daemon, tmux):
    daemon["answers"]["usage_summary"]["period"] = "fortnight"
    app, _ = make_app(cost_window="day")

    async with app.run_test():
        period = app.query_one("#usage-period", app_mod.UsagePeriodBar)
        assert period.period_label == "last 24h"


async def test_usage_summary_without_active_days_keeps_thirty_day_compatibility(daemon, tmux):
    daemon["answers"]["usage_summary"]["average"] = {"cost_microcents": 300_000_000}
    app, _ = make_app()

    async with app.run_test():
        price = app.query_one("#price-footer", app_mod.PriceFooter)
        assert price.daily_avg == 0.1


async def test_empty_average_window_reports_zero_per_active_day(daemon, tmux):
    app, _ = make_app()

    async with app.run_test():
        price = app.query_one("#price-footer", app_mod.PriceFooter)
        assert price.daily_avg == 1.0
        daemon["answers"]["usage_summary"]["average"] = {
            "cost_microcents": 0,
            "active_days": 0,
        }
        await app._refresh_usage()
        assert price.daily_avg == 0.0


async def test_usage_tiles_hover_as_whole_widgets_and_share_one_snapshot(daemon, tmux):
    app, _ = make_app()
    async with app.run_test(size=(80, 30)) as pilot:
        client = daemon["client"]
        tiles = list(app.query(app_mod.UsageMetricTile))
        assert [tile.metric for tile in tiles] == [
            "input",
            "output",
            "cache",
            "cost",
            "average",
        ]
        assert all(tile.size.height == 3 and tile.size.width > 1 for tile in tiles)
        assert client.asked("usage_by_harness") == []

        input_tile = app.query_one("#in-col", app_mod.UsageMetricTile)
        background = input_tile.background_colors
        await pilot.hover(input_tile, offset=(0, 0))
        await pilot.pause()
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        rendered = _usage_breakdown_text(panel)
        assert input_tile.has_class("-hot")
        assert not input_tile.has_pseudo_class("hover")
        assert not panel.has_pseudo_class("hover")
        assert input_tile.background_colors != background
        assert panel.has_class("-visible")
        assert "↓ input" in rendered
        assert "1k" in rendered and "2k" in rendered and "3k" in rendered
        assert client.asked("usage_by_harness") == [{}]

        output_tile = app.query_one("#out-col", app_mod.UsageMetricTile)
        await pilot.hover(output_tile, offset=(0, 0))
        await pilot.pause()
        rendered = _usage_breakdown_text(panel)
        assert output_tile.has_class("-hot")
        assert not output_tile.has_pseudo_class("hover")
        assert not input_tile.has_class("-hot")
        assert panel.has_class("-visible")
        assert "↑ output" in rendered
        assert "2k" in rendered and "5k" in rendered and "8k" in rendered
        assert client.asked("usage_by_harness") == [{}]

        await pilot.hover(panel, offset=(1, 1))
        await pilot.pause()
        assert panel.has_class("-visible")
        assert output_tile.has_class("-hot")
        assert not panel.has_pseudo_class("hover")

        await pilot.hover("#tree-panel")
        await pilot.pause()
        assert not panel.has_class("-visible")
        assert not any(tile.has_class("-hot") for tile in tiles)

        await pilot.hover("#cache-col", offset=(0, 0))
        await pilot.pause()
        assert client.asked("usage_by_harness") == [{}, {}]


async def test_usage_breakdown_rebuilds_only_when_metric_changes(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        input_tile = app.query_one("#in-col", app_mod.UsageMetricTile)
        await pilot.hover(input_tile)
        await pilot.pause()
        body = app.query_one("#usage-breakdown-content", app_mod.NonSelectableStatic)
        input_table = body.content
        assert isinstance(input_table, Table)

        input_tile.post_message(app_mod.UsageMetricTile.Hovered("input"))
        await pilot.pause()
        assert body.content is input_table

        await pilot.hover("#out-col")
        await pilot.pause()
        assert body.content is not input_table


async def test_usage_breakdown_renders_cost_average_unknown_and_old_daemon(daemon, tmux):
    daemon["answers"]["usage_by_harness"]["harnesses"].append(
        {
            "harness": "unknown",
            "today": {"cost_microcents": 0, "active_days": 1},
            "week": {"cost_microcents": 0, "active_days": 1},
            "month": {"cost_microcents": 0, "active_days": 1},
        }
    )
    app, _ = make_app()
    async with app.run_test() as pilot:
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        await pilot.hover("#price-col")
        await pilot.pause()
        rendered = _usage_breakdown_text(panel)
        assert "cost" in rendered
        assert "$1.000" in rendered and "$2.000" in rendered and "$3.000" in rendered
        assert "unknown*" in rendered and "* pre-upgrade" in rendered

        await pilot.hover("#avg-col")
        await pilot.pause()
        rendered = _usage_breakdown_text(panel)
        assert "avg/active day" in rendered
        assert rendered.count("$0.500") >= 3

        await pilot.hover("#tree-panel")
        await pilot.pause()
        daemon["answers"]["usage_by_harness"] = RemoteError("unknown_method", "old")
        await pilot.hover("#price-col")
        await pilot.pause()
        assert "restart daemon for per-harness stats" in _usage_breakdown_text(panel)


@pytest.mark.parametrize("bus_visible", [False, True])
@pytest.mark.parametrize("height", [24, 28, 30, 32, 40, 60])
async def test_usage_breakdown_overlay_sits_above_footer_without_moving_it(
    daemon, tmux, bus_visible, height
):
    app, _ = make_app(bus_visible=bus_visible)
    async with app.run_test(size=(80, height)) as pilot:
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        stack = app.query_one("#tree-stack")
        tree = app.query_one("#tree-panel", app_mod.TreePanel)
        period = app.query_one("#usage-period", app_mod.UsagePeriodBar)
        stats = app.query_one("#stats-footer", app_mod.StatsFooter)
        price = app.query_one("#price-footer", app_mod.PriceFooter)
        tree_region = tree.region
        period_region = period.region
        stats_region = stats.region
        price_region = price.region

        await pilot.hover("#in-col", offset=(0, 0))
        await pilot.pause()

        assert panel.has_class("-visible")
        assert panel.region.bottom == period.region.y
        assert panel.size.height <= 12
        assert tree.region == tree_region
        assert period.region == period_region
        assert stats.region == stats_region
        assert price.region == price_region
        assert panel.background_colors[1] != tree.background_colors[1]
        assert panel.background_colors[1] != stats.background_colors[1]
        assert panel.styles.border_bottom[0] == ""
        if bus_visible and height == 24:
            # Bus + footer exceed 24 rows; padding makes two rows irreducible.
            assert stack.size.height == 1
            assert panel.region.height == 2
            assert panel.region.y == -1
            assert panel.region.bottom == period.region.y
        elif stack.size.height >= 3:
            title = panel.query_one("#usage-breakdown-title", app_mod.NonSelectableStatic)
            assert panel.region.y >= 0
            assert panel.region.height <= stack.size.height
            assert title.region.y >= 0
        if bus_visible and height in (28, 30):
            assert panel.max_scroll_y > 0


async def test_usage_breakdown_surface_is_distinct_in_every_builtin_theme(daemon, tmux):
    app, _ = make_app()
    async with app.run_test(size=(80, 40)) as pilot:
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        tree = app.query_one("#tree-panel", app_mod.TreePanel)
        stats = app.query_one("#stats-footer", app_mod.StatsFooter)
        panel.set_class(True, "-visible")

        for theme in BUILTIN_THEMES:
            app.theme = theme
            await pilot.pause()
            panel.render_state("cost", result=daemon["answers"]["usage_by_harness"])
            background = panel.background_colors[1]
            body = panel.query_one("#usage-breakdown-content", app_mod.NonSelectableStatic)
            zebra = background.blend(body.colors[3], 0.04)
            table = body.content
            assert isinstance(table, Table)

            assert _color_delta(background, tree.background_colors[1]) >= 16, theme
            assert _color_delta(background, stats.background_colors[1]) >= 6, theme
            if background.ansi is not None or zebra.ansi is not None:
                assert len(table.row_styles) == 1, theme
            else:
                assert len(table.row_styles) == 2, theme
                assert table.row_styles[0] != table.row_styles[1], theme
                assert _color_delta(zebra, background) >= 6, theme


async def test_usage_breakdown_tracks_tree_stack_resize_without_lag(daemon, tmux):
    period = {"cost_microcents": 100_000_000, "active_days": 1}
    daemon["answers"]["usage_by_harness"] = {
        "harnesses": [
            {"harness": f"h{index}", "today": period, "week": period, "month": period}
            for index in range(15)
        ]
    }
    app, _ = make_app(bus_visible=True)
    async with app.run_test(size=(80, 60)) as pilot:
        await pilot.hover("#in-col")
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        stack = app.query_one("#tree-stack", app_mod.TreeStack)
        period_bar = app.query_one("#usage-period", app_mod.UsagePeriodBar)

        for height in (32, 30, 28, 24, 60):
            await pilot.resize_terminal(80, height)
            assert panel.region.bottom == period_bar.region.y
            if stack.size.height >= 3:
                assert panel.region.y >= 0
                assert panel.region.height <= stack.size.height

        assert panel.styles.max_height.value == app_mod.USAGE_BREAKDOWN_MAX_HEIGHT
        assert panel.region.height == app_mod.USAGE_BREAKDOWN_MAX_HEIGHT


def test_usage_breakdown_titles_use_single_cell_footer_glyphs():
    assert {glyph: cell_len(glyph) for glyph in ("↓", "↑", "⛁")} == {
        "↓": 1,
        "↑": 1,
        "⛁": 1,
    }


async def test_usage_breakdown_titles_match_each_metric(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        title = panel.query_one("#usage-breakdown-title", app_mod.NonSelectableStatic)
        result = daemon["answers"]["usage_by_harness"]
        for metric, expected in (
            ("input", "↓ input"),
            ("output", "↑ output"),
            ("cache", "⛁ cache"),
            ("cost", "cost"),
            ("average", "avg/active day"),
        ):
            panel.render_state(metric, result=result)
            assert str(title.render()) == expected


def test_usage_breakdown_compact_formatters_have_bounded_crossovers():
    assert app_mod.UsageBreakdownPanel._format_cost(999_999 * 100_000) == "$999.999"
    assert app_mod.UsageBreakdownPanel._format_cost(1_234_567 * 100_000) == "$1.2k"
    assert app_mod.PriceFooter._fmt_price(1_234.567) == "$1234.567"
    assert app_mod.UsageBreakdownPanel._format_tokens(1_500_000_000) == "1.5B"
    assert app_mod._fmt_tokens(1_500_000_000) == "1500.0M"
    assert cell_len(app_mod.UsageBreakdownPanel._format_cost(2**63 - 1)) <= 8
    assert cell_len(app_mod.UsageBreakdownPanel._format_tokens(2**63 - 1)) <= 8


async def test_usage_breakdown_narrow_zebra_table_keeps_numeric_cells_whole(daemon, tmux):
    period = {
        "cost_microcents": 99_999_900_000,
        "active_days": 1,
    }
    daemon["answers"]["usage_by_harness"] = {
        "harnesses": [
            {
                "harness": f"deliberately-long-harness-name-{index}",
                "today": period,
                "week": period,
                "month": period,
            }
            for index in range(15)
        ]
    }
    app, _ = make_app(sidebar_width=40)
    async with app.run_test(size=(80, 48)) as pilot:
        await pilot.hover("#price-col")
        await pilot.pause()
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        body = panel.query_one("#usage-breakdown-content", app_mod.NonSelectableStatic)
        table = body.content
        assert isinstance(table, Table)
        assert len(table.columns) == 4
        assert table.columns[0].min_width == 7
        assert table.columns[0].overflow == "ellipsis"
        assert all(column.width == 8 for column in table.columns[1:])
        assert all(column.justify == "right" for column in table.columns[1:])
        horizontal_padding = (len(table.columns) - 1) * (table.padding[1] + table.padding[3])
        assert 7 + horizontal_padding + sum(column.width or 0 for column in table.columns[1:]) <= 37

        rendered = _usage_breakdown_text(panel)
        assert "deliberately-long-harness-name" not in rendered
        assert "…" in rendered
        assert rendered.count("$999.999") == 45
        assert panel.region.height == 12
        assert panel.max_scroll_y > 0
        panel.scroll_down(animate=False, immediate=True)
        await pilot.pause()
        assert panel.scroll_y > 0
        assert body.allow_select is False
        assert body.get_selection(SELECT_ALL) is None
        assert len(table.row_styles) == 2
        assert table.row_styles[0] != table.row_styles[1]


async def test_late_usage_breakdown_response_cannot_reopen_after_leave(daemon, tmux):
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed(_params):
        started.set()
        await release.wait()
        return {"harnesses": []}

    daemon["answers"]["usage_by_harness"] = delayed
    app, _ = make_app()
    async with app.run_test() as pilot:
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        await pilot.hover("#in-col")
        await started.wait()
        await pilot.hover("#tree-panel")
        await pilot.pause()
        assert not panel.has_class("-visible")

        release.set()
        await pilot.pause()
        assert not panel.has_class("-visible")
        assert app._usage_breakdown is None


async def test_period_cost_and_active_day_captions_render_when_usage_refresh_fails(daemon, tmux):
    daemon["broken"].add("usage_summary")
    app, _ = make_app(cost_window="week")

    async with app.run_test():
        period = app.query_one("#usage-period", app_mod.UsagePeriodBar)
        stats = app.query_one("#stats-footer", app_mod.StatsFooter)
        price_footer = app.query_one("#price-footer", app_mod.PriceFooter)
        output = stats.query_one("#out-value", app_mod.Static)
        price = price_footer.query_one("#price-caption", app_mod.NonSelectableStatic)
        average = price_footer.query_one("#avg-caption", app_mod.NonSelectableStatic)
        assert str(period.render()) == "this week"
        assert "$text dim" in _styles(period)
        assert period.size.height == 1
        assert period.background_colors[1] == app.screen.background_colors[1]
        assert "this week" not in str(output.render())
        assert "cost" in str(price.render())
        assert "cost (week)" not in str(price.render())
        assert f"avg/active day ({USAGE_AVERAGE_WINDOW_DAYS}d)" in str(average.render())
        assert "$text dim" in _styles(price)
        assert "$text dim" in _styles(average)


async def test_only_footer_values_are_selectable():
    period = app_mod.UsagePeriodBar()
    stats = app_mod.StatsFooter()
    price = app_mod.PriceFooter()

    class FooterHost(app_mod.App):
        def compose(self):
            yield period
            yield stats
            yield price

    async with FooterHost().run_test() as pilot:
        stats._stop_timer()
        stats._display = [1_000, 2_000, 3_000]
        stats._targets = [1_000, 2_000, 3_000]
        stats._render_values()
        price._stop_timer()
        price._price_display = price._price_target = 1.25
        price._avg_display = price._avg_target = 0.5
        price._render_values()

        chrome = list(pilot.app.query(app_mod.NonSelectableStatic))
        values = list(pilot.app.query(".footer-value"))
        assert chrome
        assert len(values) == 5
        assert all(not widget.allow_select for widget in chrome)
        assert all(widget.get_selection(SELECT_ALL) is None for widget in chrome)
        assert all(widget.allow_select for widget in values)
        assert [
            str(pilot.app.query_one(f"#{prefix}-suffix", app_mod.NonSelectableStatic).render())
            for prefix in ("in", "out", "cache")
        ] == [" ↓", " ↑", " ⛁"]
        assert [
            str(pilot.app.query_one(f"#{prefix}-caption", app_mod.NonSelectableStatic).render())
            for prefix in ("in", "out", "cache")
        ] == ["input", "output", "cache"]
        assert period.size.height == 1
        assert period.background_colors[1] == pilot.app.screen.background_colors[1]
        assert stats.size.height == 3
        assert price.size.height == 3

        pilot.app.screen._select_all_in_widget(pilot.app.screen)
        assert pilot.app.screen.get_selected_text() == "1k\n2k\n3k\n$1.250\n$0.500"


async def test_price_animation_pulses_both_values_for_twenty_slower_frames(daemon, tmux):
    app, _ = make_app()
    footer = None
    async with app.run_test():
        footer = app.query_one("#price-footer", app_mod.PriceFooter)
        footer._stop_timer()
        footer._price_display = footer._price_target = 0.0
        footer._avg_display = footer._avg_target = 0.0
        footer._render_values()

        footer.totals = {"cost_microcents": 2_000_000}
        footer.daily_avg = 0.04
        assert footer._timer is not None
        assert footer._price_step == pytest.approx(0.001)
        assert footer._avg_step == pytest.approx(0.002)
        assert app_mod.FOOTER_ANIM_INTERVAL == 0.1
        assert app_mod.FOOTER_ANIM_FRAMES == 20

        footer._stop_timer()
        price = footer.query_one("#price-value", app_mod.Static)
        average = footer.query_one("#avg-value", app_mod.Static)
        price_pulse = [style for style in _styles(price) if style.startswith("#")]
        average_pulse = [style for style in _styles(average) if style.startswith("#")]
        assert price_pulse == [
            app_mod.working_harness_style(0, offset) for offset in range(len("$0.000"))
        ]
        assert average_pulse == [
            app_mod.working_harness_style(0, offset) for offset in range(len("$0.000"))
        ]

        for _ in range(19):
            footer._tick()
        assert footer._price_display == pytest.approx(0.019)
        assert footer._avg_display == pytest.approx(0.038)
        assert any(style.startswith("#") for style in _styles(price))

        footer._tick()
        assert footer._price_display == 0.02
        assert footer._avg_display == 0.04
        assert footer._timer is None
        assert not any(style.startswith("#") for style in _styles(price))
        assert not any(style.startswith("#") for style in _styles(average))

        footer._price_display = footer._price_target = 1.0
        footer.totals = {"cost_microcents": 50_000_000}
        assert footer._price_step == pytest.approx(-0.025)
        footer._stop_timer()
        footer._tick()
        assert footer._price_display == pytest.approx(0.975)

        footer.totals = {"cost_microcents": 20_000_000}
        assert footer._price_step == pytest.approx(-0.03875)
        assert footer._timer is not None

    assert footer is not None
    assert footer._timer is None


async def test_token_animation_is_independent_for_all_three_values(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        footer = app.query_one("#stats-footer", app_mod.StatsFooter)
        footer._stop_timer()
        footer._display = [5, 0, 7]
        footer._targets = [5, 0, 7]
        footer._render_values()

        footer.totals = {
            "input_tokens": 5,
            "output_tokens": 20,
            "reasoning_output_tokens": 20,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 0,
        }
        assert footer._targets == [5, 40, 7]
        assert footer._steps == [0, 2, 0]
        assert footer._timer is not None
        footer._stop_timer()

        inp = footer.query_one("#in-value", app_mod.Static)
        out = footer.query_one("#out-value", app_mod.Static)
        cache = footer.query_one("#cache-value", app_mod.Static)
        assert not any(style.startswith("#") for style in _styles(inp))
        assert any(style.startswith("#") for style in _styles(out))
        assert not any(style.startswith("#") for style in _styles(cache))

        footer._display = [0, 0, 0]
        footer._targets = [0, 0, 0]
        footer.totals = {
            "input_tokens": 20,
            "output_tokens": 40,
            "cache_read_input_tokens": 60,
        }
        assert footer._steps == [1, 2, 3]
        footer._stop_timer()
        for _ in range(20):
            footer._tick()
        assert footer._display == [20, 40, 60]
        assert footer._timer is None
        assert not any(style.startswith("#") for style in _styles(inp))
        assert not any(style.startswith("#") for style in _styles(out))
        assert not any(style.startswith("#") for style in _styles(cache))


async def test_visually_unchanged_footer_deltas_snap_without_pulsing(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        price = app.query_one("#price-footer", app_mod.PriceFooter)
        price._stop_timer()
        price._price_display = price._price_target = 0.0001
        price._avg_display = price._avg_target = 0.0
        price.totals = {"cost_microcents": 40_000}
        assert price._price_display == 0.0004
        assert price._timer is None

        stats = app.query_one("#stats-footer", app_mod.StatsFooter)
        stats._stop_timer()
        stats._display = [5_000_000, 0, 0]
        stats._targets = [5_000_000, 0, 0]
        stats.totals = {"input_tokens": 5_010_000}
        assert stats._display[0] == 5_010_000
        assert stats._timer is None


def test_footer_targets_are_retained_before_mount():
    price = app_mod.PriceFooter()
    price.totals = {"cost_microcents": 25_000_000}
    price.daily_avg = 0.2
    stats = app_mod.StatsFooter()
    stats.totals = {
        "input_tokens": 1,
        "output_tokens": 2,
        "reasoning_output_tokens": 3,
        "cache_read_input_tokens": 4,
        "cache_creation_input_tokens": 5,
    }

    assert price._price_target == 0.25
    assert price._avg_target == 0.2
    assert stats._targets == [1, 5, 9]


async def test_preloaded_stats_footer_animates_when_mounted_and_stops_when_removed():
    footer = app_mod.StatsFooter()
    was_mounted = footer.is_mounted
    footer.totals = {
        "input_tokens": 5_000_000,
        "output_tokens": 4_000_000,
        "cache_read_input_tokens": 3_000_000,
    }

    class FooterHost(app_mod.App):
        def compose(self):
            yield footer

    assert was_mounted is False
    assert footer._timer is None
    async with FooterHost().run_test():
        assert footer._targets == [5_000_000, 4_000_000, 3_000_000]
        assert footer._timer is not None
        inp = footer.query_one("#in-value", app_mod.Static)
        assert any(style.startswith("#") for style in _styles(inp))

        await footer.remove()
        assert footer._timer is None


async def test_every_tree_row_shares_one_left_inset(daemon, tmux):
    """Leaves and separators are different widgets on one visual column.

    A participant row is an AgentLeaf and the "unmanaged" divider is a plain
    Label, styled by two different rules. Nothing but this test makes them
    agree, and a one-cell disagreement is the kind of thing that reads as a
    rendering bug rather than a stylesheet one.

    The height assertion guards the other half: the leaf draws exactly three
    rows in exactly three cells, so vertical padding does not space the rows
    out, it truncates the cwd line off the bottom.
    """
    daemon["answers"]["participants.unmanaged"] = [
        {"pane": "%20", "command": "vibe", "harness": "vibe", "cwd": "/tmp/x"},
    ]
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        leaves = [w for w in panel.children if isinstance(w, app_mod.AgentLeaf)]
        labels = [w for w in panel.children if not isinstance(w, app_mod.AgentLeaf)]
        assert leaves and labels, "need both row kinds on screen to compare them"

        insets = {w.styles.padding.left for w in panel.children} | {
            w.styles.padding.right for w in panel.children
        }
        assert insets == {2}

        for leaf in leaves:
            assert leaf.styles.padding.top == 0
            assert leaf.styles.padding.bottom == 0
            assert leaf.styles.margin.top == 0
            assert leaf.styles.margin.bottom == 0
            assert leaf.styles.height.value == 3


async def test_mount_learns_its_own_window_and_session(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        assert app.my_pane == "%1"
        assert app.my_window == "@7"
        assert app.my_session_name == "work"
        # Mouse reporting is turned on for that session, not globally.
        assert ("set", "mouse", "on") in tmux
        # tmux's own status line is hidden for the duration, same scope.
        assert ("set", "status", "off") in tmux


async def test_an_empty_tree_says_so_instead_of_rendering_nothing(daemon, tmux):
    daemon["answers"]["participants.tree"] = []
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert len(panel.children) == 1
        empty = panel.query_one(app_mod.EmptyTreeState)
        assert str(empty.render()) == app_mod.EMPTY_TREE_HINT
        assert empty.region == panel.content_region
        assert empty.region.height > 1
        assert empty.region.width > len(app_mod.EMPTY_TREE_HINT)
        assert empty.styles.content_align == ("center", "middle")
        assert empty.styles.text_align == "center"
        assert _styles(empty) == [app_mod.EMPTY_TREE_SHORTCUT_STYLE, "$text-muted"]
        assert empty.allow_select is False
        assert empty.get_selection(SELECT_ALL) is None
        assert not empty.has_class("tree-cursor")
        assert not empty.has_class("tree-staged")
        assert not empty.has_class("tree-alt")


async def test_a_daemon_that_will_not_answer_does_not_stop_the_regie(daemon, tmux):
    """A refresh failure is a missing frame, not a crash: agents outlive it."""
    daemon["broken"] = {"participants.tree", "bus.tail", "harnesses"}
    app, _ = make_app()
    async with app.run_test():
        assert app.tree_lines == []
        assert app.harnesses is None  # the palette reads this as "ask locally"


async def test_an_unknown_theme_warns_and_keeps_the_default(daemon, tmux):
    app, notes = make_app(theme="not-a-theme")
    async with app.run_test():
        assert app.theme != "not-a-theme"
    assert any("unknown theme" in msg for msg, _ in notes)


async def test_a_known_theme_is_applied(daemon, tmux):
    app, notes = make_app(theme="nord")
    async with app.run_test():
        assert app.theme == "nord"
    assert not notes


# ---- cursor --------------------------------------------------------------


async def test_j_and_k_move_between_the_tree_and_footer(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        assert app.cursor == 0
        await pilot.press("k")  # already at the top
        assert app.cursor == 0
        await pilot.press("j")
        assert app.cursor == 1
        await pilot.press("j")
        assert app.cursor == 1
        assert app._usage_keyboard_metric == "input"
        await pilot.press("k")
        assert app._usage_keyboard_metric is None
        assert app.cursor == 1
        await pilot.press("k")
        assert app.cursor == 0


async def test_arrows_reach_the_app_with_a_scrollable_tree(daemon, tmux):
    daemon["answers"]["participants.tree"] = [
        {
            **PARENT,
            "id": f"{index:012x}",
            "tmux_pane": f"%{index + 10}",
            "children": [],
        }
        for index in range(40)
    ]
    app, _ = make_app()
    async with app.run_test(size=(80, 24)) as pilot:
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        breakdown = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        bus = app.query_one("#bus-panel")
        assert app.focused is None
        assert not panel.can_focus and not breakdown.can_focus and not bus.can_focus

        await pilot.press("down")
        assert app.cursor == 1
        await pilot.press("up")
        assert app.cursor == 0

        app.cursor = len(app.tree_lines) - 1
        app._render_tree()
        await pilot.press("down")
        assert app._usage_keyboard_metric == "input"
        assert not list(panel.query(".tree-cursor"))
        await app._refresh_tree()
        assert not list(panel.query(".tree-cursor"))


async def test_footer_navigation_uses_arrows_and_vim_keys(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        app.cursor = len(app.tree_lines) - 1
        await pilot.press("j")
        assert app._usage_keyboard_metric == "input"

        await pilot.press("right")
        assert app._usage_keyboard_metric == "output"
        await pilot.press("l")
        assert app._usage_keyboard_metric == "cache"
        await pilot.press("h")
        assert app._usage_keyboard_metric == "output"
        await pilot.press("left")
        assert app._usage_keyboard_metric == "input"

        for top, bottom in (
            ("input", "cost"),
            ("output", "average"),
            ("cache", "average"),
        ):
            app._select_usage_metric(top)
            await pilot.press("down")
            assert app._usage_keyboard_metric == bottom
            await pilot.press("up")
            assert app._usage_keyboard_metric == top

        app._select_usage_metric("cache")
        await pilot.press("down", "left", "up")
        assert app._usage_keyboard_metric == "input"
        await pilot.press("up")
        assert app._usage_keyboard_metric is None


async def test_keyboard_footer_reuses_snapshot_and_pointer_temporarily_overrides_it(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        app.cursor = len(app.tree_lines) - 1
        await pilot.press("down")
        await pilot.pause()
        panel = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        assert panel.has_class("-visible")
        assert app.query_one("#in-col").has_class("-hot")
        assert daemon["client"].asked("usage_by_harness") == [{}]

        await pilot.press("right")
        assert app._usage_active_metric == "output"
        assert daemon["client"].asked("usage_by_harness") == [{}]

        await pilot.hover("#cache-col")
        await pilot.pause()
        assert app._usage_active_metric == "cache"
        await pilot.hover("#tree-panel")
        await pilot.pause()
        assert app._usage_active_metric == "output"
        assert app.query_one("#out-col").has_class("-hot")
        assert daemon["client"].asked("usage_by_harness") == [{}]

        await pilot.press("up")
        assert app._usage_keyboard_metric is None
        assert not panel.has_class("-visible")


async def test_tree_actions_do_nothing_while_footer_is_selected(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        app.cursor = len(app.tree_lines) - 1
        await pilot.press("j", "enter", "x")
        assert app.staged_pane is None
        assert daemon["client"].asked("participant.kill") == []
    assert not any(call[0] in {"join", "select"} for call in tmux)


async def test_empty_tree_can_enter_and_leave_the_footer(daemon, tmux):
    daemon["answers"]["participants.tree"] = []
    app, _ = make_app()
    async with app.run_test() as pilot:
        assert app.tree_lines == []
        await pilot.press("down")
        assert app._usage_keyboard_metric == "input"
        await pilot.press("up")
        assert app._usage_keyboard_metric is None


async def test_the_cursor_line_carries_the_cursor_class(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("j")
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert not panel.children[0].has_class("tree-cursor")
        assert panel.children[1].has_class("tree-cursor")


async def test_a_shorter_tree_pulls_the_cursor_back_in(daemon, tmux):
    """The cursor cannot be left pointing past the end when an agent dies."""
    app, _ = make_app()
    async with app.run_test():
        app.cursor = 1
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        assert app.cursor == 0


# ---- staging -------------------------------------------------------------


async def test_enter_joins_the_selected_pane_and_narrows_the_regie(daemon, tmux):
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.staged_pane == "%10"
    assert ("join", "%10", "@7") in tmux
    assert ("resize", "%1", 52) in tmux
    # Silence is the contract: the pane arriving on the stage is the feedback,
    # and a toast on every Enter was noise on top of a visible result.
    assert notes == []


async def test_enter_on_the_staged_agent_unstages_it(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.staged_pane == "%10"
        await pilot.press("enter")
        assert app.staged_pane is None
    assert ("break", "%10") in tmux


async def test_staging_a_second_agent_breaks_the_first_one_out(daemon, tmux):
    """Two panes must never share the stage; the old one goes back first."""
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.press("j")
        await pilot.press("enter")
        assert app.staged_pane == "%11"
    assert tmux.index(("break", "%10")) < tmux.index(("join", "%11", "@7"))


async def test_staging_without_a_known_window_refuses(daemon, tmux):
    app, notes = make_app()
    async with app.run_test() as pilot:
        app.my_window = None
        await pilot.press("enter")
        assert app.staged_pane is None
    assert any("cannot stage" in msg for msg, _ in notes)


async def test_a_pane_that_will_not_join_is_reported_not_recorded(daemon, tmux, monkeypatch):
    async def refuse(pane, *, target_window=None):
        raise RuntimeError("no such window")

    monkeypatch.setattr(app_mod.panes, "join_pane", refuse)
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.staged_pane is None
    assert any("stage failed" in msg and sev == "error" for msg, sev in notes)


async def test_the_staged_line_is_marked_in_the_tree(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert panel.children[0].has_class("tree-staged")
        assert not panel.children[1].has_class("tree-staged")


# ---- focus ---------------------------------------------------------------


async def test_focus_selects_the_staged_pane(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.press("l")
    assert ("select", "%10") in tmux


async def test_focus_stages_first_when_nothing_is_staged(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("l")
        assert app.staged_pane == "%10"
    assert ("join", "%10", "@7") in tmux
    assert ("select", "%10") in tmux


async def test_focus_does_not_refocus_a_stale_pane_after_a_failed_switch(daemon, tmux, monkeypatch):
    async def refuse(pane, *, target_window=None):
        raise RuntimeError("no such window")

    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        assert app.staged_pane == "%10"
        monkeypatch.setattr(app_mod.panes, "join_pane", refuse)
        await pilot.press("j")
        await pilot.press("l")
    assert not any(call[0] == "select" for call in tmux)
    assert any("stage failed" in msg and sev == "error" for msg, sev in notes)


# ---- kill ----------------------------------------------------------------


async def test_kill_asks_the_daemon_and_refreshes(daemon, tmux):
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("x")
        client = daemon["client"]
        assert client.asked("participant.kill") == [{"id": PARENT["id"]}]
        # A kill changes the tree, so it is re-read rather than waited for.
        assert len(client.asked("participants.tree")) == 2
    # The row leaving the tree is the feedback; a successful kill says nothing.
    assert notes == []


async def test_a_refused_kill_is_reported(daemon, tmux):
    daemon["broken"] = {"participant.kill"}
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("x")
    assert any("kill failed" in msg and sev == "error" for msg, sev in notes)


async def test_an_unmanaged_pane_cannot_be_killed(daemon, tmux):
    """Unmanaged rows have no id, so there is nothing to address."""
    daemon["answers"]["participants.tree"] = []
    daemon["answers"]["participants.unmanaged"] = [
        {"pane": "%20", "command": "vibe", "harness": "vibe", "cwd": "/tmp/x"},
    ]
    app, notes = make_app()
    async with app.run_test() as pilot:
        await pilot.press("x")
        assert daemon["client"].asked("participant.kill") == []
    assert any(sev == "warning" for _, sev in notes)


# ---- palette spawn -------------------------------------------------------


async def test_the_palette_spawns_into_the_regie_session(daemon, tmux):
    daemon["answers"]["spawn"] = {"id": "cccccccccccc", "tmux_pane": "%30"}
    app, notes = make_app()
    async with app.run_test():
        app.spawn_harness("claude")
        await app.workers.wait_for_complete()
        [params] = daemon["client"].asked("spawn")
    # A bare CLI: no prompt, no parent, and in the window the user is looking at.
    assert params["harness"] == "claude"
    assert params["prompt"] == ""
    assert params["tmux_session"] == "work"
    # The new agent appearing in the tree is the feedback, so nothing is said.
    assert notes == []


async def test_a_failed_spawn_is_reported(daemon, tmux):
    daemon["broken"] = {"spawn"}
    app, notes = make_app()
    async with app.run_test():
        app.spawn_harness("claude")
        await app.workers.wait_for_complete()
    assert any("spawn failed" in msg and sev == "error" for msg, sev in notes)


# ---- bus -----------------------------------------------------------------


async def test_the_bus_advances_its_cursor_and_asks_only_for_new_rows(daemon, tmux):
    app, _ = make_app(bus_visible=True)
    async with app.run_test():
        daemon["answers"]["bus.tail"] = [BUS_ROW, dict(BUS_ROW, id=2)]
        await app._refresh_bus()
        assert app.bus_cursor == 2
        await app._refresh_bus()
        assert daemon["client"].asked("bus.tail")[-1]["after_id"] == 2


async def test_a_gap_in_the_feed_is_admitted(daemon, tmux):
    """Dropping events silently would make the panel lie about being complete."""
    app, _ = make_app(bus_visible=True)
    async with app.run_test():
        daemon["answers"]["bus.tail"] = [BUS_ROW]
        await app._refresh_bus()
        daemon["answers"]["bus.tail"] = [dict(BUS_ROW, id=9)]
        await app._refresh_bus()
        assert app.bus_cursor == 9
        log = app.query_one("#bus-panel", app_mod.RichLog)
        assert any("7 events dropped" in str(line) for line in log.lines)


async def test_the_bus_panel_is_hidden_until_it_is_asked_for(daemon, tmux):
    """Off unless the config says otherwise, and the panel obeys at compose.

    A reactive assigned its own default fires no watcher, so a settings-driven
    initial state has to be applied while the widget is built rather than left
    to `watch_bus_visible`. Both directions are pinned here because getting
    that wrong is invisible in one of them.
    """
    app, _ = make_app()
    async with app.run_test():
        assert app.query_one("#bus-panel", app_mod.RichLog).has_class("-hidden")

    app, _ = make_app(bus_visible=True)
    async with app.run_test():
        assert not app.query_one("#bus-panel", app_mod.RichLog).has_class("-hidden")


async def test_toggling_the_bus_panel_shows_and_hides_it(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        log = app.query_one("#bus-panel", app_mod.RichLog)
        assert log.has_class("-hidden")
        app.action_toggle_bus()
        assert not log.has_class("-hidden")
        app.action_toggle_bus()
        assert log.has_class("-hidden")


async def test_a_hidden_bus_does_not_consume_the_events_it_cannot_show(daemon, tmux):
    """A display:none RichLog keeps no writes, so the cursor must not move.

    Otherwise the panel would silently eat every event that arrived while it
    was away, and showing it would resume from a line the user never saw.
    """
    app, _ = make_app()
    async with app.run_test() as pilot:
        daemon["answers"]["bus.tail"] = [BUS_ROW, dict(BUS_ROW, id=2)]
        await app._refresh_bus()
        assert app.bus_cursor == 0
        # Showing it picks the same rows up and draws them. The pause is not
        # decoration: a RichLog that has never been displayed has no width
        # yet, and wraps every write to nothing until a layout pass gives it
        # one. In the app that frame happens long before the next poll.
        app.action_toggle_bus()
        await pilot.pause()
        await app._refresh_bus()
        assert app.bus_cursor == 2
        log = app.query_one("#bus-panel", app_mod.RichLog)
        assert log.lines


# ---- tree-route animation ------------------------------------------------


async def test_a_send_animates_while_the_bus_panel_is_hidden(daemon, tmux):
    """The animation reads the bus on its own cursor, so hiding costs nothing.

    The panel's cursor must stay where it was — it has drawn nothing — while
    the animation's own cursor moves past the row it consumed. Two readers of
    one log; this is the test that keeps them apart.
    """
    app, _ = make_app()  # bus hidden
    async with app.run_test():
        daemon["answers"]["bus.tail"] = [SEND_ROW]
        await app._refresh_anim()
        assert len(app._route_anims) == 1
        assert app.anim_cursor == 1
        assert app.bus_cursor == 0
        assert not app.query_one("#bus-panel", app_mod.RichLog).lines


async def test_the_first_poll_only_takes_the_cursor(daemon, tmux):
    """Sends already in the log happened before the régie was looking.

    Without priming, starting the régie would replay the daemon's whole
    buffer as a burst of traces for deliveries that are long finished.
    """
    daemon["answers"]["bus.tail"] = [SEND_ROW]
    app, _ = make_app()
    async with app.run_test():
        # The mount poll primed the cursor and animated nothing.
        assert app._route_anims == []
        assert app.anim_cursor == 1
        # A row arriving after that does animate.
        daemon["answers"]["bus.tail"] = [dict(SEND_ROW, id=2)]
        await app._refresh_anim()
        assert len(app._route_anims) == 1


async def test_only_sends_animate(daemon, tmux):
    """Other bus traffic moves the cursor and nothing else."""
    app, _ = make_app()
    async with app.run_test():
        daemon["answers"]["bus.tail"] = [BUS_ROW]
        await app._refresh_anim()
        assert app._route_anims == []
        assert app.anim_cursor == 1


async def test_a_send_with_no_visible_sender_or_target_is_dropped(daemon, tmux):
    """A CLI send, an external agent, a row that has died — all just skipped."""
    app, _ = make_app()
    async with app.run_test():
        app.start_route_anim(None, CHILD["id"])
        app.start_route_anim("cli", CHILD["id"])
        app.start_route_anim(PARENT["id"], "ffffffffffff")
        app.start_route_anim(PARENT["id"], PARENT["id"])
        assert app._route_anims == []
        assert app._anim_timer is None


async def test_the_trace_starts_on_the_sender_and_reaches_the_target(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        sender = panel._key_widgets[("p", PARENT["id"])]
        target = panel._key_widgets[("p", CHILD["id"])]

        app.start_route_anim(PARENT["id"], CHILD["id"])
        app._tick_route_anims()
        assert SEND_STYLE in _styles(sender)

        path = send_path(app.tree_lines, PARENT["id"], CHILD["id"])
        assert path is not None
        for _ in range(len(path) - 1):
            app._tick_route_anims()
        assert SEND_STYLE in _styles(target)


async def test_a_spawn_animates_from_parent_to_new_child(daemon, tmux):
    """participant.created refreshes the tree so the new child can receive a trace."""
    daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
    app, _ = make_app()
    async with app.run_test():
        assert len(app.tree_lines) == 1

        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD)])]
        daemon["answers"]["bus.tail"] = [SPAWN_ROW]
        await app._refresh_anim()

        assert len(app.tree_lines) == 2
        assert len(app._route_anims) == 1
        app._tick_route_anims()
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]
        assert SEND_STYLE in _styles(parent_widget)


async def test_a_promptless_spawn_does_not_animate(daemon, tmux):
    """A bare child pane should appear on the next normal tree refresh, without a trace."""
    daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
    app, _ = make_app()
    async with app.run_test():
        row = {**SPAWN_ROW, "payload": {**SPAWN_ROW["payload"], "has_prompt": False}}
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[dict(CHILD)])]
        daemon["answers"]["bus.tail"] = [row]
        await app._refresh_anim()

        assert len(app.tree_lines) == 1
        assert app._route_anims == []
        assert app.anim_cursor == 1


async def test_an_await_pulses_grey_between_caller_and_target(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        sender = panel._key_widgets[("p", PARENT["id"])]
        target = panel._key_widgets[("p", CHILD["id"])]

        daemon["answers"]["bus.tail"] = [AWAIT_START_ROW]
        await app._refresh_anim()
        assert len(app._await_anims) == 1

        app._tick_route_anims()
        # The caller is the parent here: the line departs along its own branch
        # — the only way across to the column its child's rail hangs in — then
        # drops to the child and reaches for its name.
        assert set(_overlay_glyphs(sender)) == set("┕━")
        assert set(_overlay_glyphs(target)) == set("┃┗━")
        assert (1, len(app.tree_lines[1][3])) not in target._overlay
        assert app_mod._await_route_style(0, 0) in _overlay_styles(sender)
        assert app_mod._await_route_style(0, 6) in _overlay_styles(target)
        # No bold: it would promote the grey into the bright palette and make
        # the await line brighter than the working agents it runs between.
        assert not any("bold" in style for style in _overlay_styles(target))
        assert any(style.startswith("#") for style in _overlay_styles(target))
        assert app._anim_timer is not None

        daemon["answers"]["bus.tail"] = [AWAIT_END_ROW]
        await app._refresh_anim()
        assert app._await_anims == {}
        assert app._anim_timer is None
        assert sender._overlay is None
        assert target._overlay is None


async def test_a_child_awaiting_its_parent_draws_the_dashes_on_the_parent(daemon, tmux):
    """The other direction of the same edge, and the only source of ``┕``.

    The route arrives along the parent's own branch from the right, so no
    extension is added and the last cell drawn is the corner itself.
    """
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]

        app.start_await_anim("token", "handle", CHILD["id"], PARENT["id"])
        app._tick_route_anims()

        assert set(_overlay_glyphs(parent_widget)) == set("┕━")
        assert str(parent_widget.render()).split("\n")[1].startswith("┕━━ ")


async def test_the_await_line_reads_as_one_line_from_caller_to_awaited(daemon, tmux):
    """The whole point, read off the screen rather than off the coordinates.

    A parent awaiting its child: the parent's own branch turns heavy, the rail
    between them drops, and the child's branch reaches for its name. One
    unbroken line — bar the cwd row, where the parent's text occupies the
    column the child's rail would continue in and the line is dashed.
    """
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        sender = panel._key_widgets[("p", PARENT["id"])]
        target = panel._key_widgets[("p", CHILD["id"])]
        plain = [str(sender.render()).split("\n"), str(target.render()).split("\n")]

        app.start_await_anim("token", "handle", PARENT["id"], CHILD["id"])
        app._tick_route_anims()
        drawn = [str(sender.render()).split("\n"), str(target.render()).split("\n")]

        assert drawn[0][1].startswith("┕━━ ")  # the caller departs along its own branch
        assert drawn[1][0].startswith("    ┃")  # the rail down to the child
        assert drawn[1][1].startswith("    ┗━━ ")  # into the awaited child
        # Only the rails moved: everything from the status glyph rightwards,
        # and the cwd rows, are the characters the tree drew.
        assert drawn[0][1][4:] == plain[0][1][4:]
        assert drawn[0][2] == plain[0][2]
        assert drawn[1][1][8:] == plain[1][1][8:]
        assert drawn[1][2] == plain[1][2]


async def test_an_await_whose_end_row_never_comes_reaps_itself(daemon, tmux):
    """A missed `job.await.end` must not leave a pulse running for the session.

    `bus.tail` returns only the newest rows after the cursor, so the end row
    can be dropped outright — and the daemon can die mid-await. Either way the
    pulse has to expire on its own.
    """
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        target = panel._key_widgets[("p", CHILD["id"])]

        daemon["answers"]["bus.tail"] = [AWAIT_START_ROW]
        await app._refresh_anim()
        app._tick_route_anims()
        assert len(app._await_anims) == 1
        assert target._overlay is not None

        anim = next(iter(app._await_anims.values()))
        anim.started -= app_mod.AWAIT_ANIM_TTL + 1

        app._tick_route_anims()
        assert app._await_anims == {}
        assert app._anim_timer is None
        assert target._overlay is None


async def test_an_expired_await_does_not_keep_a_slot_from_a_live_one(daemon, tmux):
    """The cap counts live pulses; a stale one is reaped before it turns one away."""
    app, _ = make_app()
    async with app.run_test():
        for index in range(app_mod.MAX_AWAIT_ANIMS):
            app.start_await_anim(f"token-{index}", "handle", PARENT["id"], CHILD["id"])
        assert len(app._await_anims) == app_mod.MAX_AWAIT_ANIMS

        app.start_await_anim("one-too-many", "handle", PARENT["id"], CHILD["id"])
        assert len(app._await_anims) == app_mod.MAX_AWAIT_ANIMS

        for anim in app._await_anims.values():
            anim.started -= app_mod.AWAIT_ANIM_TTL + 1
        app.start_await_anim("after-the-reaping", "handle", PARENT["id"], CHILD["id"])
        assert len(app._await_anims) == 1


async def test_a_batch_of_rows_refreshes_the_tree_once(daemon, tmux):
    """One daemon round-trip per batch, not one per row.

    A burst of spawns used to pay for the same answer several times over, in
    sequence, on the frame that could least afford it.
    """
    app, _ = make_app()
    async with app.run_test():
        before = len(daemon["client"].asked("participants.tree"))
        daemon["answers"]["bus.tail"] = [
            dict(SPAWN_ROW, id=10),
            dict(AWAIT_START_ROW, id=11),
            dict(SPAWN_ROW, id=12),
        ]
        await app._refresh_anim()
        assert len(daemon["client"].asked("participants.tree")) == before + 1


async def test_the_await_route_is_found_once_per_tree_revision(daemon, tmux):
    """Ten frames a second over a tree that changes once a second: cache it."""
    app, _ = make_app()
    async with app.run_test():
        calls: list[tuple] = []
        real = app_mod.await_highlight_cells

        def counted(lines, from_id, to_id):
            calls.append((from_id, to_id))
            return real(lines, from_id, to_id)

        app_mod.await_highlight_cells = counted  # type: ignore[assignment]
        try:
            app.start_await_anim("token", "handle", PARENT["id"], CHILD["id"])
            app._tick_route_anims()
            app._tick_route_anims()
            app._tick_route_anims()
            assert len(calls) == 1

            await app._refresh_tree()  # the tree moved; the route may have too
            app._tick_route_anims()
            assert len(calls) == 2
        finally:
            app_mod.await_highlight_cells = real  # type: ignore[assignment]


async def test_cross_root_trace_walks_every_cell_to_the_other_roots_child(daemon, tmux):
    """Long root-to-child routes must not be squeezed into a fixed frame count."""
    other_root = {
        **PARENT,
        "id": "cccccccccccc",
        "name": "other-root",
        "tmux_pane": "%12",
        "children": [dict(CHILD)],
    }
    daemon["answers"]["participants.tree"] = [dict(PARENT, children=[]), other_root]
    app, _ = make_app()
    async with app.run_test():
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        path = send_path(app.tree_lines, PARENT["id"], CHILD["id"])
        assert path is not None
        assert len(path) > 13

        app.start_route_anim(PARENT["id"], CHILD["id"])
        for expected in path:
            app._tick_route_anims()
            leaf_index, row_in_leaf = app_mod.cell_leaf(expected)
            key = app.tree_lines[leaf_index][2]
            widget = panel._key_widgets[key]
            assert isinstance(widget, app_mod.AgentLeaf)
            assert set(widget._overlay or {}) == {(row_in_leaf, expected[1])}
            assert next(iter((widget._overlay or {}).values())) in "━┃┏┓┗┛"


async def test_the_animation_timer_runs_only_while_something_is_in_flight(daemon, tmux):
    """It starts on the first trace and stops with the last one, leaving no glyph."""
    app, _ = make_app()
    async with app.run_test():
        assert app._anim_timer is None
        app.start_route_anim(PARENT["id"], CHILD["id"])
        assert app._anim_timer is not None

        path = send_path(app.tree_lines, PARENT["id"], CHILD["id"])
        assert path is not None
        for _ in range(len(path) + 1):
            app._tick_route_anims()

        assert app._route_anims == []
        assert app._anim_timer is None
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        assert panel._overlaid == set()
        for widget in panel._key_widgets.values():
            assert SEND_STYLE not in _styles(widget)


async def test_two_sends_animate_at_once(daemon, tmux):
    """Concurrent rather than queued: a trace shown late lies about when it landed."""
    app, _ = make_app()
    async with app.run_test():
        app.start_route_anim(PARENT["id"], CHILD["id"])
        app.start_route_anim(CHILD["id"], PARENT["id"])
        assert len(app._route_anims) == 2
        app._tick_route_anims()
        assert len(app._route_anims) == 2


async def test_a_flood_of_sends_is_capped(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        for _ in range(app_mod.MAX_TRACE_ANIMS + 5):
            app.start_route_anim(PARENT["id"], CHILD["id"])
        assert len(app._route_anims) == app_mod.MAX_TRACE_ANIMS


async def test_a_trace_whose_participant_vanishes_is_dropped_cleanly(daemon, tmux):
    """The tree refreshes every second; an animation must survive losing an end."""
    app, _ = make_app()
    async with app.run_test():
        app.start_route_anim(PARENT["id"], CHILD["id"])
        app._tick_route_anims()
        daemon["answers"]["participants.tree"] = [dict(PARENT, children=[])]
        await app._refresh_tree()
        app._tick_route_anims()
        assert app._route_anims == []
        assert app._anim_timer is None


async def test_a_daemon_that_will_not_answer_leaves_the_animation_alone(daemon, tmux):
    app, notes = make_app()
    async with app.run_test():
        daemon["broken"] = {"bus.tail"}
        await app._refresh_anim()
        assert app._route_anims == []
    assert notes == []


# ---- exit ----------------------------------------------------------------


async def test_quitting_unstages_and_gives_the_mouse_back(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.press("q")
    assert ("break", "%10") in tmux
    # No prior session-local value, so ours is removed rather than pinned.
    assert ("unset", "mouse") in tmux
    assert ("unset", "status") in tmux
    assert daemon["client"].closed


# ---- sidebar width --------------------------------------------------------


async def test_configured_sidebar_width_reaches_both_style_and_resize(daemon, tmux):
    """The width is read once and used twice: the #sidebar style and resize_pane."""
    app, _ = make_app(sidebar_width=44)
    async with app.run_test() as pilot:
        sidebar = app.query_one("#sidebar")
        assert sidebar.styles.width.value == 44
        await pilot.press("enter")
    assert ("resize", "%1", 44) in tmux


# ---- mouse --------------------------------------------------------------


async def test_single_click_moves_the_cursor(daemon, tmux):
    """A single click on a leaf moves the cursor to that participant."""
    app, _ = make_app()
    async with app.run_test(size=(80, 40)) as pilot:
        assert app.cursor == 0
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        await pilot.click(widget=child_widget)
        assert app.cursor == 1


async def test_double_click_stages_the_agent(daemon, tmux):
    """A double click on a leaf stages it, the same as pressing enter."""
    app, _ = make_app()
    async with app.run_test() as pilot:
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]
        await pilot.click(widget=parent_widget, times=2)
        assert app.staged_pane == "%10"
    assert ("join", "%10", "@7") in tmux


async def test_click_on_any_row_of_a_leaf_moves_the_cursor(daemon, tmux):
    """All three rows of a leaf are one click target."""
    app, _ = make_app()
    async with app.run_test(size=(80, 40)) as pilot:
        assert app.cursor == 0
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        child_widget = panel._key_widgets[("p", CHILD["id"])]
        # Click at offset (0, 2) — the third row (cwd), still inside the leaf.
        await pilot.click(widget=child_widget, offset=(0, 2))
        assert app.cursor == 1


async def test_tree_click_takes_cursor_back_from_footer(daemon, tmux):
    app, _ = make_app()
    async with app.run_test(size=(80, 40)) as pilot:
        app.cursor = len(app.tree_lines) - 1
        await pilot.press("j")
        breakdown = app.query_one("#usage-breakdown", app_mod.UsageBreakdownPanel)
        panel = app.query_one("#tree-panel", app_mod.TreePanel)
        parent_widget = panel._key_widgets[("p", PARENT["id"])]

        await pilot.click(widget=parent_widget)
        assert app._usage_keyboard_metric is None
        assert not breakdown.has_class("-visible")
        assert app.cursor == 0
        assert parent_widget.has_class("tree-cursor")

        app.cursor = len(app.tree_lines) - 1
        await pilot.press("j")
        await pilot.click(widget=parent_widget, times=2)
        assert app.staged_pane == "%10"
    assert ("join", "%10", "@7") in tmux
