"""Styled content rendering for animated dashboard text."""

from __future__ import annotations

from collections.abc import Sequence

from textual.content import Content

from theater.constants.regie import (
    REGIE_DASHBOARD_CURSOR_GLYPH,
    REGIE_DASHBOARD_CURSOR_STYLE,
    REGIE_DASHBOARD_HARNESS_AVAILABLE_GLYPH,
    REGIE_DASHBOARD_HARNESS_AVAILABLE_STYLE,
    REGIE_DASHBOARD_HARNESS_UNAVAILABLE_GLYPH,
    REGIE_DASHBOARD_HARNESS_UNAVAILABLE_STYLE,
    REGIE_DASHBOARD_SENTENCES,
    REGIE_DASHBOARD_TIP_CURSOR_STYLE,
    REGIE_DASHBOARD_TIP_HEADING_STYLE,
    REGIE_DASHBOARD_TIP_HIGHLIGHT_STYLE,
    REGIE_DASHBOARD_TIP_STYLE,
)
from theater.harness import describe
from theater.regie.animations.reveal import StyledPart, clip_parts


def animated_text_content(
    parts: Sequence[StyledPart],
    visible: int,
    *,
    cursor: bool = False,
    cursor_style: str = REGIE_DASHBOARD_CURSOR_STYLE,
) -> Content:
    """Clip styled text to a visible prefix and optionally append its cursor."""
    clipped = clip_parts(parts, visible)
    if cursor:
        clipped = [*clipped, (REGIE_DASHBOARD_CURSOR_GLYPH, cursor_style)]
    return Content.assemble(*clipped) if clipped else Content.assemble("")


def sentence_parts(configured: Sequence[str] | None) -> tuple[tuple[StyledPart, ...], ...]:
    """Return configured plain sentences or the styled built-in corpus."""
    if configured is None:
        return REGIE_DASHBOARD_SENTENCES
    return tuple((sentence,) for sentence in configured)


def dashboard_tip_window_content(
    items: Sequence[Sequence[StyledPart]],
    *,
    incoming_visible: int | None = None,
    cursor: bool = False,
) -> Content:
    """Render one active tip and dimmed upcoming tips."""
    assembled: list[StyledPart] = [("Tips", REGIE_DASHBOARD_TIP_HEADING_STYLE)]
    last = len(items) - 1
    for index, item in enumerate(items):
        assembled.append("\n")
        active = index == 0
        prefix_style = REGIE_DASHBOARD_TIP_HIGHLIGHT_STYLE if active else REGIE_DASHBOARD_TIP_STYLE
        assembled.append(("› " if active else "  ", prefix_style))
        visible_item = (
            clip_parts(item, incoming_visible)
            if index == last and incoming_visible is not None
            else list(item)
        )
        for part in visible_item:
            text, style = (part, REGIE_DASHBOARD_TIP_STYLE) if isinstance(part, str) else part
            assembled.append((text, style if active else f"{style} dim"))
        if index == last and incoming_visible is not None and cursor:
            assembled.append((REGIE_DASHBOARD_CURSOR_GLYPH, REGIE_DASHBOARD_TIP_CURSOR_STYLE))
    return Content.assemble(*assembled)


def harness_availability_content(rows: list[dict] | None) -> Content:
    """Render one compact availability line per plugged-in harness."""
    source = describe() if rows is None else rows
    parts: list[StyledPart] = []
    for row in source:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if parts:
            parts.append("\n")
        available = bool(row.get("installed", True)) and not row.get("error")
        glyph = (
            REGIE_DASHBOARD_HARNESS_AVAILABLE_GLYPH
            if available
            else REGIE_DASHBOARD_HARNESS_UNAVAILABLE_GLYPH
        )
        style = (
            REGIE_DASHBOARD_HARNESS_AVAILABLE_STYLE
            if available
            else REGIE_DASHBOARD_HARNESS_UNAVAILABLE_STYLE
        )
        parts.append((f"{glyph} {name}", style))
    return Content.assemble(*parts) if parts else Content.assemble("")
