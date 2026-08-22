"""Usage footer widgets: token stats, price, period bar, and animation helpers.

Three footer widgets (``UsageMetricTile``, ``UsagePeriodBar``,
``PriceFooter``, ``StatsFooter``) plus the shared animation functions
they use. The footer animates value changes with the same working-harness
grayscale pulse the tree uses for working agents.
"""

from __future__ import annotations

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from theater.constants import MICROCENTS_PER_DOLLAR, USAGE_AVERAGE_WINDOW_DAYS
from theater.constants.regie import (
    REGIE_FOOTER_ANIM_DURATION,
    REGIE_FOOTER_ANIM_FRAMES,
    REGIE_FOOTER_ANIM_INTERVAL,
)
from theater.regie.animations.footer import _advance_float, _advance_int, _pulsing_value
from theater.regie.animations.pulse import advance_pulse_frame
from theater.regie.widgets.chrome import NonSelectableStatic

FOOTER_ANIM_INTERVAL = REGIE_FOOTER_ANIM_INTERVAL
FOOTER_ANIM_DURATION = REGIE_FOOTER_ANIM_DURATION
FOOTER_ANIM_FRAMES = REGIE_FOOTER_ANIM_FRAMES


def _fmt_tokens(n: int) -> str:
    """Human-readable token count."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


class UsageMetricTile(Vertical):
    """A full footer column that reports hover transitions to the app."""

    DEFAULT_CSS = """
    UsageMetricTile {
        width: 1fr;
        height: 3;
    }
    UsageMetricTile.-hot {
        background: $accent 10%;
    }
    """

    class Hovered(Message):
        def __init__(self, metric: str) -> None:
            super().__init__()
            self.metric = metric

    class Left(Message):
        pass

    def __init__(self, metric: str, *children: Widget, **kwargs) -> None:
        super().__init__(*children, **kwargs)
        self.metric = metric

    def on_enter(self, _event: events.Enter) -> None:
        self.post_message(self.Hovered(self.metric))

    def on_leave(self, _event: events.Leave) -> None:
        self.post_message(self.Left())


class UsagePeriodBar(NonSelectableStatic):
    """One-line period caption above the usage footers."""

    DEFAULT_CSS = """
    UsagePeriodBar {
        height: 1;
        text-align: center;
        content-align: center middle;
        /* Transparent so this row reads with the tree background above it. */
    }
    """

    period_label: reactive[str] = reactive("today")

    def watch_period_label(self, _label: str) -> None:
        if self.is_mounted:
            self._render_label()

    def _render_label(self) -> None:
        self.update(Content.assemble((self.period_label, "$text dim")))

    def on_mount(self) -> None:
        self._render_label()


class PriceFooter(Widget):
    """Price row — two centered columns via CSS, no manual padding."""

    DEFAULT_CSS = """
    PriceFooter {
        height: 3;
        layout: horizontal;
    }
    PriceFooter > .footer-column {
        width: 1fr;
        height: 3;
    }
    PriceFooter .footer-row {
        width: 1fr;
        height: 1;
        text-align: center;
        content-align: center bottom;
    }
    #price-col {
        color: $text;
    }
    #avg-col {
        color: $text;
    }
    """

    totals: reactive[dict | None] = reactive(None)
    daily_avg: reactive[float] = reactive(0.0)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._price_display = 0.0
        self._price_target = 0.0
        self._price_step = 0.0
        self._avg_display = 0.0
        self._avg_target = 0.0
        self._avg_step = 0.0
        self._frame = 0
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with UsageMetricTile("cost", id="price-col", classes="footer-column"):
            yield NonSelectableStatic("", classes="footer-row footer-spacer")
            yield Static("", id="price-value", classes="footer-row footer-value")
            yield NonSelectableStatic(
                Content.assemble(("cost", "$text dim")),
                id="price-caption",
                classes="footer-row",
            )
        with UsageMetricTile("average", id="avg-col", classes="footer-column"):
            yield NonSelectableStatic("", classes="footer-row footer-spacer")
            yield Static("", id="avg-value", classes="footer-row footer-value")
            yield NonSelectableStatic(
                Content.assemble((f"avg/active day ({USAGE_AVERAGE_WINDOW_DAYS}d)", "$text dim")),
                id="avg-caption",
                classes="footer-row",
            )

    def watch_totals(self, totals: dict | None) -> None:
        if not isinstance(totals, dict):
            return
        self._price_target = totals.get("cost_microcents", 0) / MICROCENTS_PER_DOLLAR
        if self.is_mounted:
            self._prepare_animation()

    def watch_daily_avg(self, val: float) -> None:
        self._avg_target = val
        if self.is_mounted:
            self._prepare_animation()

    @staticmethod
    def _fmt_price(value: float) -> str:
        return f"${value:.3f}"

    @staticmethod
    def _fmt_avg(value: float) -> str:
        return f"${value:.3f}"

    def _price_active(self) -> bool:
        return self._fmt_price(self._price_display) != self._fmt_price(self._price_target)

    def _avg_active(self) -> bool:
        return self._fmt_avg(self._avg_display) != self._fmt_avg(self._avg_target)

    def _render_values(self) -> None:
        self.query_one("#price-value", Static).update(
            _pulsing_value(
                self._fmt_price(self._price_display),
                frame=self._frame,
                active=self._price_active(),
                value_style="$text bold",
            )
        )
        self.query_one("#avg-value", Static).update(
            _pulsing_value(
                self._fmt_avg(self._avg_display),
                frame=self._frame,
                active=self._avg_active(),
                value_style="$text",
            )
        )

    def _prepare_animation(self) -> None:
        self._stop_timer()
        if not self._price_active():
            self._price_display = self._price_target
        if not self._avg_active():
            self._avg_display = self._avg_target
        self._price_step = (self._price_target - self._price_display) / FOOTER_ANIM_FRAMES
        self._avg_step = (self._avg_target - self._avg_display) / FOOTER_ANIM_FRAMES
        self._frame = 0
        self._render_values()
        if self._price_active() or self._avg_active():
            self._start_timer()

    def _tick(self) -> None:
        self._frame = advance_pulse_frame(self._frame)
        if self._price_active():
            self._price_display = _advance_float(
                self._price_display, self._price_target, self._price_step, self._fmt_price
            )
        if self._avg_active():
            self._avg_display = _advance_float(
                self._avg_display, self._avg_target, self._avg_step, self._fmt_avg
            )
        self._render_values()
        if not self._price_active() and not self._avg_active():
            self._stop_timer()

    def _start_timer(self) -> None:
        if self._timer is not None:
            return
        self._timer = self.set_interval(FOOTER_ANIM_INTERVAL, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def on_mount(self) -> None:
        self._prepare_animation()

    def on_unmount(self) -> None:
        self._stop_timer()


class StatsFooter(Widget):
    """Token stats row — three centered columns via CSS."""

    DEFAULT_CSS = """
    StatsFooter {
        height: 3;
        layout: horizontal;
        background: $surface;
        color: $text-muted;
    }
    StatsFooter > .footer-column {
        width: 1fr;
        height: 3;
    }
    StatsFooter .footer-row {
        width: 1fr;
        height: 1;
        text-align: center;
        content-align: center bottom;
        padding: 0;
        border: none;
    }
    StatsFooter .footer-value-row {
        width: 1fr;
        height: 1;
        align: center middle;
    }
    StatsFooter .footer-value-row > Static {
        width: auto;
        height: 1;
        color: $text;
    }
    """

    totals: reactive[dict | None] = reactive(None)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._display = [0, 0, 0]
        self._targets = [0, 0, 0]
        self._steps = [0, 0, 0]
        self._frame = 0
        self._timer: Timer | None = None

    def compose(self) -> ComposeResult:
        for prefix, metric, suffix, caption in (
            ("in", "input", " ↓", "input"),
            ("out", "output", " ↑", "output"),
            ("cache", "cache", " ⛁", "cache"),
        ):
            with UsageMetricTile(metric, id=f"{prefix}-col", classes="footer-column"):
                yield NonSelectableStatic("", classes="footer-row footer-spacer")
                with Horizontal(classes="footer-value-row"):
                    yield Static("", id=f"{prefix}-value", classes="footer-value")
                    yield NonSelectableStatic(suffix, id=f"{prefix}-suffix")
                yield NonSelectableStatic(
                    Content.assemble((caption, "$text dim")),
                    id=f"{prefix}-caption",
                    classes="footer-row",
                )

    def watch_totals(self, totals: dict | None) -> None:
        if not isinstance(totals, dict):
            return
        self._targets = [
            int(totals.get("input_tokens", 0)),
            int(totals.get("output_tokens", 0)) + int(totals.get("reasoning_output_tokens", 0)),
            int(totals.get("cache_read_input_tokens", 0))
            + int(totals.get("cache_creation_input_tokens", 0)),
        ]
        if self.is_mounted:
            self._prepare_animation()

    def _active(self, index: int) -> bool:
        return _fmt_tokens(self._display[index]) != _fmt_tokens(self._targets[index])

    def _render_values(self) -> None:
        for index, selector in enumerate(("#in-value", "#out-value", "#cache-value")):
            self.query_one(selector, Static).update(
                _pulsing_value(
                    _fmt_tokens(self._display[index]),
                    frame=self._frame,
                    active=self._active(index),
                    value_style="$text",
                )
            )

    def _prepare_animation(self) -> None:
        self._stop_timer()
        for index, target in enumerate(self._targets):
            if not self._active(index):
                self._display[index] = target
            difference = target - self._display[index]
            magnitude = (abs(difference) + FOOTER_ANIM_FRAMES - 1) // FOOTER_ANIM_FRAMES
            self._steps[index] = magnitude if difference >= 0 else -magnitude
        self._frame = 0
        self._render_values()
        if any(self._active(index) for index in range(3)):
            self._start_timer()

    def _tick(self) -> None:
        self._frame = advance_pulse_frame(self._frame)
        for index in range(3):
            if self._active(index):
                self._display[index] = _advance_int(
                    self._display[index], self._targets[index], self._steps[index], _fmt_tokens
                )
        self._render_values()
        if not any(self._active(index) for index in range(3)):
            self._stop_timer()

    def _start_timer(self) -> None:
        if self._timer is None:
            self._timer = self.set_interval(FOOTER_ANIM_INTERVAL, self._tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def on_mount(self) -> None:
        self._prepare_animation()

    def on_unmount(self) -> None:
        self._stop_timer()
