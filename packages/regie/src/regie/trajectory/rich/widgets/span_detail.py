"""Navigable detail canvas for one trajectory span or tool operation."""

from __future__ import annotations

from typing import ClassVar

from rich.console import Console, ConsoleOptions, RenderResult
from rich.padding import Padding
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Label, LoadingIndicator, RichLog

from regie.telemetry import (
    REGIE_TRAJECTORY_DETAIL_PROJECT,
    REGIE_TRAJECTORY_DETAIL_RENDER,
    span,
)
from regie.trajectory.domain import ParticipantLink, TrajectoryRecord, TrajectoryRequest
from regie.trajectory.domain.tools import TrajectoryToolOperation
from regie.trajectory.rich.inspection.content import Palette
from regie.trajectory.rich.inspection.links import (
    DETAIL_PARTICIPANT_EXACT_META,
    DETAIL_PARTICIPANT_META,
    DETAIL_PARTICIPANT_UNRESOLVED_META,
    DETAIL_RECORD_TARGET_META,
    participant_link_from_meta,
)
from regie.trajectory.rich.inspection.sheet import RecordLookup, Section, SpanSheet, build_sheet
from regie.trajectory.ui_constants import TRAJECTORY_DETAIL_FOLD_LINES

DETAIL_SECTION_META = "trajectory_detail_section"
_PALETTE_ROLES = ("text", "muted", "accent", "key", "string", "number", "error", "success")


class SpanDetailCopyRequested(Message):
    """A section or the whole page was copied."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class SpanDetailParticipantLinkClicked(Message):
    """A participant link in the detail content was activated."""

    def __init__(self, link: ParticipantLink, *, exact: bool, unresolved: bool) -> None:
        super().__init__()
        self.link = link
        self.participant_id = link.participant_id
        self.target_record_id = link.target_record_id
        self.exact = exact
        self.unresolved = unresolved


class SpanDetailRecordLinkClicked(Message):
    """A record link in the detail content was activated."""

    def __init__(self, record_id: str) -> None:
        super().__init__()
        self.record_id = record_id


class _DetailLoadingIndicator(LoadingIndicator):
    def _on_mount(self, event: events.Mount) -> None:
        super()._on_mount(event)
        self.auto_refresh = None

    def set_active(self, active: bool) -> None:
        self.display = active
        self.auto_refresh = 1 / 16 if active else None


class _Lines:
    """Lines rendered once at a known width, written to the log unchanged."""

    def __init__(self, lines: list[list[Segment]]) -> None:
        self._lines = lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for line in self._lines:
            yield from line
            yield Segment.line()


class SpanDetailPanel(Vertical):
    """A header of facts above foldable sections navigated with h/l and Enter."""

    can_focus = True
    COMPONENT_CLASSES: ClassVar[set[str]] = Widget.COMPONENT_CLASSES | {
        f"span-detail--{role}" for role in _PALETTE_ROLES
    }

    # Foreground colours only: content always sits on the panel's own background.
    DEFAULT_CSS = """
    SpanDetailPanel {
        width: 1fr;
        height: 1fr;
        min-height: 0;
        background: $background;
    }
    SpanDetailPanel > #trajectory-span-detail-header {
        width: 1fr;
        height: auto;
        padding: 1 2;
        background: $foreground 4%;
    }
    SpanDetailPanel:focus-within > #trajectory-span-detail-header {
        background: $accent 18%;
    }
    SpanDetailPanel #trajectory-span-detail-title,
    SpanDetailPanel #trajectory-span-detail-meta {
        width: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    SpanDetailPanel > #trajectory-span-detail-body {
        width: 1fr;
        height: 1fr;
        min-height: 0;
        layers: detail-content detail-loading;
    }
    SpanDetailPanel RichLog {
        layer: detail-content;
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        background: $background;
        scrollbar-size: 1 1;
    }
    SpanDetailPanel RichLog:focus {
        background-tint: transparent;
    }
    SpanDetailPanel LoadingIndicator {
        display: none;
        position: absolute;
        layer: detail-loading;
        width: 1fr;
        height: 1fr;
        color: $accent;
        background: $background;
    }
    SpanDetailPanel > .span-detail--text { color: $foreground; }
    SpanDetailPanel > .span-detail--muted { color: $text-muted; }
    SpanDetailPanel > .span-detail--accent { color: $accent; }
    SpanDetailPanel > .span-detail--key { color: $primary; }
    SpanDetailPanel > .span-detail--string { color: $success; }
    SpanDetailPanel > .span-detail--number { color: $warning; }
    SpanDetailPanel > .span-detail--error { color: $error; }
    SpanDetailPanel > .span-detail--success { color: $success; }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._record: TrajectoryRecord | None = None
        self._tool: TrajectoryToolOperation | None = None
        self._request: TrajectoryRequest | None = None
        self._sheet: SpanSheet | None = None
        self._selected = 0
        self._toggled: set[str] = set()
        self._expanded: set[str] = set()
        self._heading_lines: list[int] = []
        self._cache: dict[tuple[str, int], list[list[Segment]]] = {}
        self._rendered_width = 0
        self._reflow_pending = False

    def compose(self) -> ComposeResult:
        with Vertical(id="trajectory-span-detail-header"):
            yield Label("No span selected", id="trajectory-span-detail-title")
            yield Label("", id="trajectory-span-detail-meta")
        with Vertical(id="trajectory-span-detail-body"):
            yield RichLog(id="trajectory-span-detail-log", min_width=1, wrap=False)
            yield _DetailLoadingIndicator(id="trajectory-span-detail-loading")

    # ---- state -----------------------------------------------------------------------

    @property
    def record_id(self) -> str | None:
        return self._record.record_id if self._record is not None else None

    @property
    def sections(self) -> tuple[Section, ...]:
        return self._sheet.sections if self._sheet is not None else ()

    @property
    def selected_section(self) -> Section | None:
        sections = self.sections
        return sections[self._selected] if sections else None

    @property
    def copy_text(self) -> str:
        """The selected section; `page_copy_text` has everything."""
        section = self.selected_section
        return section.copy_text if section is not None else ""

    @property
    def page_copy_text(self) -> str:
        return self._sheet.copy_text if self._sheet is not None else ""

    def is_folded(self, section: Section) -> bool:
        return section.folded != (section.key in self._toggled)

    def _palette(self) -> Palette:
        return Palette(
            **{
                role: self.get_component_rich_style(f"span-detail--{role}", partial=True)
                for role in _PALETTE_ROLES
            }
        )

    # ---- updates ---------------------------------------------------------------------

    def set_span(
        self,
        record: TrajectoryRecord,
        *,
        tool: TrajectoryToolOperation | None = None,
        request: TrajectoryRequest | None = None,
        lookup: RecordLookup | None = None,
    ) -> None:
        if (record, tool, request) == (self._record, self._tool, self._request):
            self.hide_pending()
            return
        same_span = self._record is not None and self._record.record_id == record.record_id
        self._record, self._tool, self._request = record, tool, request
        with span(REGIE_TRAJECTORY_DETAIL_PROJECT, tab="page"):
            self._sheet = build_sheet(
                record, self._palette(), tool=tool, request=request, lookup=lookup
            )
        self._cache.clear()
        if not same_span:
            self._selected = 0
            self._toggled.clear()
            self._expanded.clear()
        self._selected = min(self._selected, max(0, len(self.sections) - 1))
        self._sync_header()
        self._schedule_reflow(keep_scroll=same_span)

    def _sync_header(self) -> None:
        if not self.is_mounted or self._sheet is None:
            return
        self.query_one("#trajectory-span-detail-title", Label).update(self._sheet.title)
        meta = self.query_one("#trajectory-span-detail-meta", Label)
        meta.display = self._sheet.meta is not None
        meta.update(self._sheet.meta or "")

    def move_section(self, delta: int) -> None:
        if not self.sections:
            return
        self._selected = max(0, min(len(self.sections) - 1, self._selected + delta))
        self._write(keep_scroll=True)
        self._reveal_selected()

    def toggle_section(self) -> None:
        """Fold or unfold the selected section; a long unfolded one expands in full first."""
        section = self.selected_section
        if section is None:
            return
        body_lines = len(self._body_lines(section, self._rendered_width))
        if (
            not self.is_folded(section)
            and section.long_folds
            and body_lines > TRAJECTORY_DETAIL_FOLD_LINES
            and section.key not in self._expanded
        ):
            self._expanded.add(section.key)
        else:
            self._toggled.symmetric_difference_update({section.key})
            self._expanded.discard(section.key)
        self._write(keep_scroll=True)
        self._reveal_selected()

    def scroll_content(self, delta: int) -> None:
        self._log().scroll_relative(y=delta, animate=False)

    def show_pending(self) -> None:
        """Cover the details with the loading state until the next span is shown."""
        for indicator in self.query("#trajectory-span-detail-loading").results(
            _DetailLoadingIndicator
        ):
            indicator.set_active(True)

    def hide_pending(self) -> None:
        for indicator in self.query("#trajectory-span-detail-loading").results(
            _DetailLoadingIndicator
        ):
            indicator.set_active(False)

    # ---- rendering -------------------------------------------------------------------

    def _log(self) -> RichLog:
        return self.query_one("#trajectory-span-detail-log", RichLog)

    def on_resize(self, _event: events.Resize) -> None:
        self._schedule_reflow(keep_scroll=True)

    def _schedule_reflow(self, *, keep_scroll: bool) -> None:
        if self._reflow_pending or not self.is_mounted:
            return
        self._reflow_pending = True
        if not self.call_after_refresh(self._write, keep_scroll=keep_scroll):
            self._reflow_pending = False

    def _body_lines(self, section: Section, width: int) -> list[list[Segment]]:
        key = (section.key, width)
        if key not in self._cache:
            console = self.app.console
            options = console.options.update(width=max(1, width), height=None)
            body = Padding(section.body, (0, 0, 0, 2))
            self._cache[key] = console.render_lines(body, options, pad=False, new_lines=False)
        return self._cache[key]

    def _write(self, *, keep_scroll: bool = False) -> None:
        self._reflow_pending = False
        if not self.is_attached or self._sheet is None:
            self.hide_pending()
            return
        log = self._log()
        width = log.scrollable_content_region.width
        if width <= 0:
            self.hide_pending()
            return
        scroll_y = float(log.scroll_y) if keep_scroll else 0.0
        with span(REGIE_TRAJECTORY_DETAIL_RENDER, tab="page"):
            lines = self._page_lines(width)
            log.clear()
            log.write(_Lines(lines), width=width, scroll_end=False)
        self._rendered_width = width
        log.scroll_to(y=scroll_y, animate=False, force=True)
        self.hide_pending()

    def _page_lines(self, width: int) -> list[list[Segment]]:
        palette = self._palette()
        page: list[list[Segment]] = []
        self._heading_lines = []
        for index, section in enumerate(self.sections):
            if index:
                page.append([])
            self._heading_lines.append(len(page))
            body = self._body_lines(section, width)
            page.append(self._line(self._heading(index, section, len(body), palette), width))
            if self.is_folded(section):
                continue
            if (
                section.long_folds
                and len(body) > TRAJECTORY_DETAIL_FOLD_LINES
                and section.key not in self._expanded
            ):
                page.extend(body[:TRAJECTORY_DETAIL_FOLD_LINES])
                hidden = len(body) - TRAJECTORY_DETAIL_FOLD_LINES
                more = Text(f"  … {hidden} more lines · ⏎ to expand", style=palette.muted)
                more.stylize(Style(meta={DETAIL_SECTION_META: index}))
                page.append(self._line(more, width))
            else:
                page.extend(body)
        return page

    def _line(self, text: Text, width: int) -> list[Segment]:
        console = self.app.console
        lines = console.render_lines(text, console.options.update(width=width), pad=False)
        return lines[0] if lines else []

    def _heading(self, index: int, section: Section, lines: int, palette: Palette) -> Text:
        selected = index == self._selected
        folded = self.is_folded(section)
        heading = Text(no_wrap=True, overflow="ellipsis")
        heading.append("▌ " if selected else "  ", style=palette.accent)
        heading.append("▸ " if folded else "▾ ", style=palette.muted)
        title_style = (palette.accent if selected else palette.text) + Style(bold=True)
        heading.append(section.title.upper(), style=title_style)
        if folded:
            heading.append(f"   {lines} lines", style=palette.muted)
        heading.stylize(Style(meta={DETAIL_SECTION_META: index}))
        return heading

    def _reveal_selected(self) -> None:
        if not self._heading_lines:
            return
        log = self._log()
        top = self._heading_lines[self._selected]
        height = log.scrollable_content_region.height
        if top < log.scroll_y or top >= log.scroll_y + height - 2:
            log.scroll_to(y=max(0, top - 1), animate=False, force=True)

    # ---- pointer --------------------------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        meta = event.style.meta
        section = meta.get(DETAIL_SECTION_META)
        if isinstance(section, int):
            event.stop()
            self._selected = section
            self.toggle_section()
            return
        if link := participant_link_from_meta(meta):
            event.stop()
            self.post_message(
                SpanDetailParticipantLinkClicked(
                    link,
                    exact=meta.get(DETAIL_PARTICIPANT_EXACT_META) == "1",
                    unresolved=meta.get(DETAIL_PARTICIPANT_UNRESOLVED_META) == "1",
                )
            )
            return
        participant_id = meta.get(DETAIL_PARTICIPANT_META)
        if isinstance(participant_id, str):
            event.stop()
            self.post_message(
                SpanDetailParticipantLinkClicked(
                    ParticipantLink(participant_id, "related"), exact=False, unresolved=False
                )
            )
            return
        record_id = meta.get(DETAIL_RECORD_TARGET_META)
        if isinstance(record_id, str):
            event.stop()
            self.post_message(SpanDetailRecordLinkClicked(record_id))


__all__ = [
    "DETAIL_SECTION_META",
    "SpanDetailCopyRequested",
    "SpanDetailPanel",
    "SpanDetailParticipantLinkClicked",
    "SpanDetailRecordLinkClicked",
]
