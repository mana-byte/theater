"""Navigable detail canvas for one trajectory span or tool operation."""

from __future__ import annotations

from dataclasses import dataclass
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
from regie.trajectory.rich.inspection.content import NODE_META, Palette
from regie.trajectory.rich.inspection.links import (
    DETAIL_PARTICIPANT_EXACT_META,
    DETAIL_PARTICIPANT_META,
    DETAIL_PARTICIPANT_UNRESOLVED_META,
    DETAIL_RECORD_TARGET_META,
    participant_link_from_meta,
)
from regie.trajectory.rich.inspection.sheet import RecordLookup, Section, SpanSheet, build_sheet
from regie.trajectory.ui_constants import (
    TRAJECTORY_DETAIL_BODY_INDENT,
    TRAJECTORY_DETAIL_FOLD_LINES,
    TRAJECTORY_DETAIL_ROLE_COLORS,
)

DETAIL_SECTION_META = "trajectory_detail_section"
_PALETTE_ROLES = ("text", "muted", "accent", "key", "string", "number", "error", "success")
_ROLES = TRAJECTORY_DETAIL_ROLE_COLORS
# Each kind of section gets its own tint; blending keeps it on the theme's background.
_ROLE_CSS = "\n".join(
    f"    SpanDetailPanel > .span-detail--head-{role} {{ background: {color} 22%; }}\n"
    f"    SpanDetailPanel > .span-detail--head-{role}-cursor {{ background: {color} 50%; }}"
    for role, color in _ROLES.items()
)


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


@dataclass(frozen=True, slots=True)
class _Item:
    """A navigable line: a section heading, or a foldable data branch (`node`)."""

    line: int
    section: int
    node: str | None


class _Lines:
    """Lines rendered once at a known width, written to the log unchanged."""

    def __init__(self, lines: list[list[Segment]]) -> None:
        self._lines = lines

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        for line in self._lines:
            yield from line
            yield Segment.line()


class SpanDetailPanel(Vertical):
    """Section headings and long data branches, walked with h/l and folded with Enter."""

    can_focus = True
    COMPONENT_CLASSES: ClassVar[set[str]] = Widget.COMPONENT_CLASSES | {
        *(f"span-detail--{role}" for role in _PALETTE_ROLES),
        "span-detail--cursor",
        *(f"span-detail--head-{role}{state}" for role in _ROLES for state in ("", "-cursor")),
    }

    DEFAULT_CSS = f"""
    SpanDetailPanel {{
        width: 1fr;
        height: 1fr;
        min-height: 0;
        background: $background;
    }}
    SpanDetailPanel > #trajectory-span-detail-header {{
        width: 1fr;
        height: auto;
        padding: 1 2;
        background: $foreground 4%;
    }}
    SpanDetailPanel:focus-within > #trajectory-span-detail-header {{
        background: $accent 18%;
    }}
    SpanDetailPanel #trajectory-span-detail-title,
    SpanDetailPanel #trajectory-span-detail-meta {{
        width: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }}
    SpanDetailPanel > #trajectory-span-detail-body {{
        width: 1fr;
        height: 1fr;
        min-height: 0;
        layers: detail-content detail-loading;
    }}
    SpanDetailPanel RichLog {{
        layer: detail-content;
        width: 1fr;
        height: 1fr;
        padding: 1 2;
        background: $background;
        scrollbar-size: 1 1;
    }}
    SpanDetailPanel RichLog:focus {{
        background-tint: transparent;
    }}
    SpanDetailPanel LoadingIndicator {{
        display: none;
        position: absolute;
        layer: detail-loading;
        width: 1fr;
        height: 1fr;
        color: $accent;
        background: $background;
    }}
    SpanDetailPanel > .span-detail--text {{ color: $foreground; }}
    SpanDetailPanel > .span-detail--muted {{ color: $text-muted; }}
    SpanDetailPanel > .span-detail--accent {{ color: $accent; }}
    SpanDetailPanel > .span-detail--key {{ color: $primary; }}
    SpanDetailPanel > .span-detail--string {{ color: $success; }}
    SpanDetailPanel > .span-detail--number {{ color: $warning; }}
    SpanDetailPanel > .span-detail--error {{ color: $error; }}
    SpanDetailPanel > .span-detail--success {{ color: $success; }}
    SpanDetailPanel > .span-detail--cursor {{ background: $foreground 14%; }}
{_ROLE_CSS}
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._record: TrajectoryRecord | None = None
        self._tool: TrajectoryToolOperation | None = None
        self._request: TrajectoryRequest | None = None
        self._sheet: SpanSheet | None = None
        self._cursor = 0
        self._items: list[_Item] = []
        self._toggled: set[str] = set()
        self._toggled_nodes: set[str] = set()
        self._expanded: set[str] = set()
        self._cache: dict[tuple[str, int, frozenset[str]], list[list[Segment]]] = {}
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
        """The section holding the cursor."""
        sections = self.sections
        if not sections:
            return None
        index = self._items[self._cursor].section if self._items else 0
        return sections[min(index, len(sections) - 1)]

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
            self._cursor = 0
            self._toggled.clear()
            self._toggled_nodes.clear()
            self._expanded.clear()
        self._sync_header()
        self._schedule_reflow(keep_scroll=same_span)

    def _sync_header(self) -> None:
        if not self.is_mounted or self._sheet is None:
            return
        self.query_one("#trajectory-span-detail-title", Label).update(self._sheet.title)
        meta = self.query_one("#trajectory-span-detail-meta", Label)
        meta.display = self._sheet.meta is not None
        meta.update(self._sheet.meta or "")

    def move(self, delta: int) -> None:
        """Move the cursor to the previous or next section heading or data branch."""
        if not self._items:
            return
        self._cursor = max(0, min(len(self._items) - 1, self._cursor + delta))
        self._write(keep_scroll=True)
        self._reveal_cursor()

    def toggle(self) -> None:
        """Fold or unfold what the cursor is on; a partly shown section expands first."""
        if not self._items:
            return
        item = self._items[self._cursor]
        if item.node is not None:
            self._toggled_nodes.symmetric_difference_update({item.node})
            self._expanded.add(self.sections[item.section].key)  # an opened branch shows whole
        else:
            self._toggle_section(self.sections[item.section])
        self._write(keep_scroll=True)
        self._reveal_cursor()

    def _toggle_section(self, section: Section) -> None:
        clipped = (
            not self.is_folded(section)
            and section.long_folds
            and len(self._body_lines(section, self._rendered_width)) > TRAJECTORY_DETAIL_FOLD_LINES
            and section.key not in self._expanded
        )
        if clipped:
            self._expanded.add(section.key)
        else:
            self._toggled.symmetric_difference_update({section.key})
            self._expanded.discard(section.key)

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
        prefix = f"{section.key}/"
        toggled = frozenset(node for node in self._toggled_nodes if node.startswith(prefix))
        key = (section.key, width, toggled)
        if key not in self._cache:
            console = self.app.console
            options = console.options.update(width=max(1, width), height=None)
            # Bodies sit under their heading's title, not flush with the bar.
            body = Padding(section.render(toggled), (0, 0, 0, TRAJECTORY_DETAIL_BODY_INDENT))
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
        """Lay out every section, index the foldable lines, and mark the cursor's."""
        palette = self._palette()
        cursor = self._items[self._cursor] if self._items else None
        page: list[list[Segment]] = []
        items: list[_Item] = []
        for index, section in enumerate(self.sections):
            if index:
                page.append([])
            items.append(_Item(len(page), index, None))
            page.append([])  # the heading, drawn once the cursor is placed
            body = self._body_lines(section, width)
            if self.is_folded(section):
                continue
            clipped = (
                section.long_folds
                and len(body) > TRAJECTORY_DETAIL_FOLD_LINES
                and section.key not in self._expanded
            )
            shown = body[:TRAJECTORY_DETAIL_FOLD_LINES] if clipped else body
            for line in shown:
                node = next(
                    (
                        seg.style.meta[NODE_META]
                        for seg in line
                        if seg.style is not None and NODE_META in seg.style.meta
                    ),
                    None,
                )
                if node is not None:
                    items.append(_Item(len(page), index, node))
                page.append(line)
            if clipped:
                hidden = len(body) - TRAJECTORY_DETAIL_FOLD_LINES
                indent = " " * TRAJECTORY_DETAIL_BODY_INDENT
                more = Text(f"{indent}… {hidden} more lines · ⏎ to expand", style=palette.muted)
                page.append(self._line(more, width))
        self._items = items
        self._cursor = self._find(cursor)
        for position, item in enumerate(items):
            on_cursor = position == self._cursor
            if item.node is None:
                section = self.sections[item.section]
                count = len(self._body_lines(section, width))
                page[item.line] = self._heading(item.section, section, count, width, on_cursor)
            elif on_cursor:
                page[item.line] = self._highlight(page[item.line], width)
        return page

    def _find(self, previous: _Item | None) -> int:
        """Keep the cursor on the same heading or branch across re-renders."""
        if previous is None:
            return 0
        for position, item in enumerate(self._items):
            if (item.section, item.node) == (previous.section, previous.node):
                return position
        return next(
            (
                position
                for position, item in enumerate(self._items)
                if item.section == previous.section
            ),
            0,
        )

    def _line(self, text: Text, width: int, style: Style | None = None) -> list[Segment]:
        console = self.app.console
        options = console.options.update(width=width)
        lines = console.render_lines(text, options, style=style, pad=style is not None)
        return lines[0] if lines else []

    def _heading(
        self, index: int, section: Section, lines: int, width: int, on_cursor: bool
    ) -> list[Segment]:
        """A full-width bar tinted by what the section holds; the cursor deepens it."""
        state = "-cursor" if on_cursor else ""
        bar = self.get_component_rich_style(f"span-detail--head-{section.role}{state}")
        folded = self.is_folded(section)
        heading = Text(no_wrap=True, overflow="ellipsis")
        heading.append("▌" if on_cursor else " ", style=Style(bold=True))
        heading.append(" ▸ " if folded else " ▾ ")
        heading.append(section.title.upper(), style=Style(bold=True))
        if folded:
            heading.append(f"   {lines} lines", style=Style(dim=True))
        heading.stylize(Style(meta={DETAIL_SECTION_META: index}))
        return self._line(heading, width, bar)

    def _highlight(self, line: list[Segment], width: int) -> list[Segment]:
        cursor = self.get_component_rich_style("span-detail--cursor")
        used = sum(segment.cell_length for segment in line)
        return [
            *(Segment(seg.text, (seg.style or Style()) + cursor, seg.control) for seg in line),
            Segment(" " * max(0, width - used), cursor),
        ]

    def _reveal_cursor(self) -> None:
        if not self._items:
            return
        log = self._log()
        top = self._items[self._cursor].line
        height = log.scrollable_content_region.height
        if top < log.scroll_y or top >= log.scroll_y + height - 2:
            log.scroll_to(y=max(0, top - 1), animate=False, force=True)

    # ---- pointer --------------------------------------------------------------------

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button != 1:
            return
        meta = event.style.meta
        section, node = meta.get(DETAIL_SECTION_META), meta.get(NODE_META)
        if isinstance(section, int) or isinstance(node, str):
            event.stop()
            self._cursor = next(
                (
                    position
                    for position, item in enumerate(self._items)
                    if (item.node, item.section) == (node, section)
                    or (node is not None and item.node == node)
                ),
                self._cursor,
            )
            self.toggle()
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
