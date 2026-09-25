"""Per-harness usage table overlaid above the footer."""

from __future__ import annotations

from typing import ClassVar

from rich.console import Group
from rich.style import Style
from rich.table import Table
from textual import events
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.geometry import Spacing
from textual.message import Message

from regie.formatting import format_cost, format_tokens
from regie.ui_constants import (
    REGIE_USAGE_BREAKDOWN_GROUP_SPACER_ROWS,
    REGIE_USAGE_BREAKDOWN_HARNESS_STYLE,
    REGIE_USAGE_BREAKDOWN_LABEL_MIN_WIDTH,
    REGIE_USAGE_BREAKDOWN_MODEL_INDENT,
    REGIE_USAGE_BREAKDOWN_MODEL_STYLE,
    REGIE_USAGE_BREAKDOWN_NUMERIC_WIDTH,
    REGIE_USAGE_BREAKDOWN_TABLE_PADDING,
    REGIE_USAGE_BREAKDOWN_TOTAL_STYLE,
    REGIE_USAGE_BREAKDOWN_UNKNOWN_MODEL_MARKER,
    REGIE_USAGE_TOP_PARTICIPANTS,
    REGIE_USAGE_TOP_PARTICIPANTS_TITLE_STYLE,
)
from regie.widgets.chrome import NonSelectableStatic


class UsageBreakdownPanel(VerticalScroll):
    """Hover-driven per-harness usage table."""

    can_focus = False

    DEFAULT_CSS = """
    UsageBreakdownPanel {
        display: none;
        dock: bottom;
        layer: overlay;
        width: 100%;
        height: auto;
        padding: 1;
        background: $accent 8%;
        scrollbar-size: 1 1;
    }
    UsageBreakdownPanel.-visible { display: block; }
    UsageBreakdownPanel > Static { width: 100%; height: auto; }
    #usage-breakdown-title { color: $text-accent; text-style: bold; }
    #usage-breakdown-note { color: $text-muted; }
    """

    _METRIC_TITLES: ClassVar[dict[str, str]] = {
        "input": "input",
        "output": "output",
        "cache": "cache",
        "cost": "cost",
        "average": "avg/active day",
    }
    _METRIC_GLYPHS: ClassVar[dict[str, str]] = {
        "input": "↓",
        "output": "↑",
        "cache": "⛁",
    }

    class Left(Message):
        pass

    def __init__(self, *args, **kwargs) -> None:
        self._normal_padding: Spacing | None = None
        super().__init__(*args, **kwargs)

    def compose(self) -> ComposeResult:
        yield NonSelectableStatic("", id="usage-breakdown-title")
        yield NonSelectableStatic("", id="usage-breakdown-content")
        yield NonSelectableStatic("", id="usage-breakdown-note")

    def on_leave(self, _event: events.Leave) -> None:
        self.post_message(self.Left())

    def constrain_to_height(self, height: int) -> None:
        if self._normal_padding is None:
            self._normal_padding = self.styles.padding
        if height < self._normal_padding.height:
            self.styles.padding = (0, self._normal_padding.left)
        else:
            self.styles.padding = self._normal_padding
        self.styles.max_height = height

    @classmethod
    def _format_metric(cls, metric: str, period: dict) -> str:
        if metric == "input":
            return format_tokens(int(period.get("input_tokens", 0)))
        if metric == "output":
            value = int(period.get("output_tokens", 0)) + int(
                period.get("reasoning_output_tokens", 0)
            )
            return format_tokens(value)
        if metric == "cache":
            value = int(period.get("cache_read_input_tokens", 0)) + int(
                period.get("cache_creation_input_tokens", 0)
            )
            return format_tokens(value)
        cost = float(period.get("cost_microcents", 0))
        if metric == "average":
            active_days = int(period.get("active_days", 0))
            cost = cost / active_days if active_days > 0 else 0
        return format_cost(cost)

    def render_state(
        self,
        metric: str,
        *,
        result: dict | None = None,
        message: str | None = None,
        detailed: bool = False,
        participants: list[dict] | None = None,
    ) -> None:
        title = self._METRIC_TITLES[metric]
        glyph = self._METRIC_GLYPHS.get(metric)
        self.query_one("#usage-breakdown-title", NonSelectableStatic).update(
            f"{glyph} {title}" if glyph else title
        )
        content = self.query_one("#usage-breakdown-content", NonSelectableStatic)
        note = self.query_one("#usage-breakdown-note", NonSelectableStatic)
        note.update("")
        rows = result.get("harnesses") if isinstance(result, dict) else None
        detailed_payload = detailed and self._is_detailed_result(result)
        if not isinstance(rows, list) or (message is not None and result is None):
            content.update(Content.assemble((message or "loading…", "$text-muted")))
            return
        if detailed_payload:
            assert isinstance(result, dict)
            table = self._detailed_table(content, metric, rows, result)
        else:
            table = self._compact_table(content, metric, rows)
        participant_table = self._top_participants_table(participants or [])
        content.update(Group(table, participant_table) if participant_table is not None else table)

        notes: list[str] = []
        if any(isinstance(row, dict) and row.get("harness") == "unknown" for row in rows):
            notes.append("* pre-upgrade")
        if detailed_payload and any(
            isinstance(row, dict)
            and any(
                isinstance(model, dict) and model.get("model") is None
                for model in row.get("models", [])
            )
            for row in rows
        ):
            notes.append(f"{REGIE_USAGE_BREAKDOWN_UNKNOWN_MODEL_MARKER} model not recorded")
        if message is not None:
            notes.append(message)
        note.update(" · ".join(notes))

    @classmethod
    def _top_participants_table(cls, rows: list[dict]) -> Table | None:
        ranked = sorted(
            rows,
            key=lambda row: int(row.get("cost_microcents", 0)),
            reverse=True,
        )[:REGIE_USAGE_TOP_PARTICIPANTS]
        if not ranked:
            return None
        table = Table(
            title="Top participants",
            title_style=REGIE_USAGE_TOP_PARTICIPANTS_TITLE_STYLE,
            box=None,
            expand=True,
            pad_edge=False,
            padding=REGIE_USAGE_BREAKDOWN_TABLE_PADDING,
            header_style=Style(dim=True),
        )
        table.add_column("name", ratio=1, overflow="ellipsis")
        table.add_column("harness", overflow="ellipsis")
        for heading in ("in", "out", "cost"):
            table.add_column(heading, justify="right", no_wrap=True)
        for row in ranked:
            table.add_row(
                str(row.get("name") or row.get("participant_id") or "unknown"),
                str(row.get("harness") or "unknown"),
                format_tokens(int(row.get("input_tokens", 0))),
                format_tokens(
                    int(row.get("output_tokens", 0)) + int(row.get("reasoning_output_tokens", 0))
                ),
                format_cost(float(row.get("cost_microcents", 0))),
            )
        return table

    @staticmethod
    def _is_detailed_result(result: dict | None) -> bool:
        if not isinstance(result, dict):
            return False
        rows = result.get("harnesses")
        return (
            isinstance(rows, list)
            and isinstance(result.get("totals"), dict)
            and all(isinstance(row, dict) and isinstance(row.get("models"), list) for row in rows)
        )

    @staticmethod
    def _add_columns(table: Table) -> None:
        table.add_column(
            "harness",
            ratio=1,
            min_width=REGIE_USAGE_BREAKDOWN_LABEL_MIN_WIDTH,
            no_wrap=True,
            overflow="ellipsis",
        )
        for heading in ("today", "week", "month"):
            table.add_column(
                heading,
                width=REGIE_USAGE_BREAKDOWN_NUMERIC_WIDTH,
                justify="right",
                no_wrap=True,
                overflow="crop",
            )

    @classmethod
    def _new_table(cls, content: NonSelectableStatic) -> Table:
        panel_background = content.background_colors[1]
        zebra_background = panel_background.blend(content.colors[3], 0.04)
        row_styles = (
            (Style.null(), Style(bgcolor=zebra_background.rich_color))
            if panel_background.ansi is None and zebra_background.ansi is None
            else (Style.null(),)
        )
        table = Table(
            box=None,
            expand=True,
            pad_edge=False,
            padding=REGIE_USAGE_BREAKDOWN_TABLE_PADDING,
            header_style=Style(dim=True),
            row_styles=row_styles,
        )
        cls._add_columns(table)
        return table

    def _compact_table(self, content: NonSelectableStatic, metric: str, rows: list) -> Table:
        table = self._new_table(content)
        for row in rows:
            if not isinstance(row, dict):
                continue
            harness = str(row.get("harness", "unknown"))
            label = "unknown*" if harness == "unknown" else harness
            values = [
                self._format_metric(metric, row.get(period, {}))
                for period in ("today", "week", "month")
            ]
            table.add_row(label, *values)
        return table

    def _detailed_table(
        self, content: NonSelectableStatic, metric: str, rows: list, result: dict
    ) -> Table:
        table = self._new_table(content)
        rendered_harnesses = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if rendered_harnesses:
                for _ in range(REGIE_USAGE_BREAKDOWN_GROUP_SPACER_ROWS):
                    table.add_row("", "", "", "")
            harness = str(row.get("harness", "unknown"))
            label = "unknown*" if harness == "unknown" else harness
            values = [
                self._format_metric(metric, row.get(period, {}))
                for period in ("today", "week", "month")
            ]
            table.add_row(label, *values, style=REGIE_USAGE_BREAKDOWN_HARNESS_STYLE)
            rendered_harnesses += 1
            models = row.get("models", [])
            if not isinstance(models, list):
                continue
            for model_row in models:
                if not isinstance(model_row, dict):
                    continue
                model = model_row.get("model")
                model_label = (
                    f"unknown model{REGIE_USAGE_BREAKDOWN_UNKNOWN_MODEL_MARKER}"
                    if model is None
                    else str(model)
                )
                model_label = f"{REGIE_USAGE_BREAKDOWN_MODEL_INDENT}{model_label}"
                values = [
                    self._format_metric(metric, model_row.get(period, {}))
                    for period in ("today", "week", "month")
                ]
                table.add_row(model_label, *values, style=REGIE_USAGE_BREAKDOWN_MODEL_STYLE)

        totals = result.get("totals", {})
        if not isinstance(totals, dict):
            totals = {}
        total_values = [
            self._format_metric(metric, totals.get(period, {}))
            for period in ("today", "week", "month")
        ]
        table.add_row("total", *total_values, style=REGIE_USAGE_BREAKDOWN_TOTAL_STYLE)
        return table


UsageBreakdown = UsageBreakdownPanel

__all__ = ["UsageBreakdown", "UsageBreakdownPanel"]
