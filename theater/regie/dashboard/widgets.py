"""Animated text widgets for the unstaged régie dashboard."""

from __future__ import annotations

from collections.abc import Sequence

from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.content import Content
from textual.timer import Timer

from theater.constants.regie import (
    REGIE_DASHBOARD_CURSOR_STYLE,
    REGIE_DASHBOARD_TIP_CURSOR_STYLE,
    REGIE_DASHBOARD_TIPS,
)
from theater.regie.animations.cycling_text import CyclingTextController, CyclingTextFrame
from theater.regie.animations.reveal import StyledPart
from theater.regie.dashboard.content import (
    animated_text_content,
    harness_availability_content,
    sentence_parts,
)
from theater.regie.widgets.chrome import NonSelectableStatic


class AnimatedDashboardText(NonSelectableStatic):
    """One independently timed type-in, hold, and type-out text cycle."""

    def __init__(
        self,
        items: Sequence[Sequence[StyledPart]],
        *,
        hold_seconds: float,
        char_interval: float,
        cursor_style: str = REGIE_DASHBOARD_CURSOR_STYLE,
        click_to_advance: bool = False,
        randomize: bool = False,
        paused: bool = False,
        **kwargs,
    ) -> None:
        self._controller = CyclingTextController(
            items,
            hold=hold_seconds,
            char_interval=char_interval,
            randomize=randomize,
        )
        self._cursor_style = cursor_style
        self._click_to_advance = click_to_advance
        self._paused = paused
        self._timer: Timer | None = None
        super().__init__(self._content(0), **kwargs)

    @property
    def controller(self) -> CyclingTextController:
        return self._controller

    @property
    def timer(self) -> Timer | None:
        return self._timer

    def _content(self, visible: int, *, cursor: bool = False) -> Content:
        return animated_text_content(
            self._controller.parts,
            visible,
            cursor=cursor,
            cursor_style=self._cursor_style,
        )

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    def _schedule(self, delay: float) -> None:
        self._stop_timer()
        if not self._paused and self._controller.active:
            self._timer = self.set_timer(delay, self._tick)

    def _render_frame(self, frame: CyclingTextFrame) -> None:
        self.update(self._content(frame.visible, cursor=frame.cursor), layout=False)

    def _tick(self) -> None:
        self._timer = None
        if self._paused or not self.is_mounted:
            return
        frame = self._controller.tick()
        self._render_frame(frame)
        self._schedule(frame.next_delay)

    def advance(self) -> bool:
        """Advance immediately and begin typing the next item with one timer."""
        if self._paused or not self._controller.active:
            return False
        self._stop_timer()
        self._controller.advance()
        self._tick()
        return True

    def set_paused(self, paused: bool) -> None:
        """Stop hidden work or resume from the current animation phase."""
        if paused == self._paused:
            return
        self._paused = paused
        if paused:
            self._stop_timer()
            self.update(self._content(self._controller.visible), layout=False)
        else:
            self._schedule(self._controller.resume_delay)

    def on_click(self, event: events.Click) -> None:
        if self._click_to_advance:
            event.stop()
            self.advance()

    def on_mount(self) -> None:
        self._schedule(self._controller.initial_delay)

    def on_unmount(self) -> None:
        self._stop_timer()


class WelcomeDashboard(Vertical):
    """Centered animated sentence and clickable tip, hidden while staged."""

    DEFAULT_CSS = """
    WelcomeDashboard {
        width: 1fr;
        min-width: 0;
        height: 1fr;
        layers: copy harnesses;
        background: $background;
    }
    WelcomeDashboard.-staged {
        display: none;
    }
    WelcomeDashboard > #dashboard-copy {
        width: 100%;
        height: 100%;
        align: center middle;
        layer: copy;
    }
    WelcomeDashboard AnimatedDashboardText {
        width: 100%;
        height: auto;
        min-height: 1;
        padding: 0 2;
        text-align: center;
    }
    WelcomeDashboard #dashboard-sentence {
        margin-bottom: 2;
        color: $text;
    }
    WelcomeDashboard #dashboard-tip {
        color: $text-muted;
    }
    WelcomeDashboard > #dashboard-harnesses {
        dock: bottom;
        layer: harnesses;
        width: auto;
        height: auto;
        margin: 0 0 1 1;
        text-align: left;
    }
    """

    def __init__(
        self,
        harnesses: list[dict] | None = None,
        *,
        sentences: Sequence[str] | None = None,
        sentence_hold_seconds: float = 10.0,
        sentence_char_interval: float = 0.1,
        tip_hold_seconds: float = 6.0,
        tip_char_interval: float = 0.04,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._harnesses = harnesses
        self._sentences = sentences
        self._sentence_hold_seconds = sentence_hold_seconds
        self._sentence_char_interval = sentence_char_interval
        self._tip_hold_seconds = tip_hold_seconds
        self._tip_char_interval = tip_char_interval
        self._staged = False

    def compose(self) -> ComposeResult:
        with Vertical(id="dashboard-copy"):
            sentences = sentence_parts(self._sentences)
            if sentences:
                yield AnimatedDashboardText(
                    sentences,
                    hold_seconds=self._sentence_hold_seconds,
                    char_interval=self._sentence_char_interval,
                    randomize=True,
                    id="dashboard-sentence",
                )
            yield AnimatedDashboardText(
                REGIE_DASHBOARD_TIPS,
                hold_seconds=self._tip_hold_seconds,
                char_interval=self._tip_char_interval,
                cursor_style=REGIE_DASHBOARD_TIP_CURSOR_STYLE,
                click_to_advance=True,
                id="dashboard-tip",
            )
        yield NonSelectableStatic(
            harness_availability_content(self._harnesses),
            id="dashboard-harnesses",
        )

    def update_harnesses(self, rows: list[dict] | None) -> None:
        """Replace the harness snapshot without disturbing text animations."""
        self._harnesses = rows
        if self.is_mounted:
            self.query_one("#dashboard-harnesses", NonSelectableStatic).update(
                harness_availability_content(rows),
                layout=False,
            )

    def set_staged(self, staged: bool) -> None:
        """Hide the dashboard and pause both text cycles while a pane is staged."""
        if staged == self._staged:
            return
        self._staged = staged
        self.set_class(staged, "-staged")
        for text in self.query(AnimatedDashboardText):
            text.set_paused(staged)
