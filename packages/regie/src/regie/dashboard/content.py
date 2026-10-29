"""Styled content rendering for animated dashboard text."""

from __future__ import annotations

import re
from collections.abc import Sequence

from textual.content import Content

from regie.motions.reveal import StyledPart, clip_parts
from regie.ui_constants import (
    REGIE_DASHBOARD_COMPAT_RANGE_MAX,
    REGIE_DASHBOARD_COMPAT_REASON_MAX,
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

_COMPATIBILITY_STATUS_STYLES = {
    "native-compatible": REGIE_DASHBOARD_HARNESS_AVAILABLE_STYLE,
    "outside-qualified-range": "$warning",
    "legacy-only": "$text-muted",
    "unknown": "$warning dim",
}

_STATUS_GLYPHS = {
    "native-compatible": REGIE_DASHBOARD_HARNESS_AVAILABLE_GLYPH,
    "outside-qualified-range": "!",
    "legacy-only": "–",
    "unknown": "?",
}

_BOUND_PAIR = re.compile(r"^>=(\d[\d.]*),(<=?)(\d[\d.]*)$")


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


def _clip_line(text: str, limit: int) -> str:
    """Clip one line to a bounded width without ever raising."""
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _version_tuple(text: str) -> tuple[int, ...] | None:
    parts = text.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _compact_bound_pair(segment: str) -> str | None:
    """Compact one `>=A,<B` / `>=A,<=B` pair into `A–last` (or `A` for a single release)."""
    match = _BOUND_PAIR.match(segment.replace(" ", ""))
    if match is None:
        return None
    low, high = _version_tuple(match.group(1)), _version_tuple(match.group(3))
    if low is None or high is None or len(low) != len(high) or len(low) < 2:
        return None
    if match.group(2) == "<=":
        last = ".".join(str(part) for part in high)
    elif high[-1] > 0:
        last = ".".join(str(part) for part in (*high[:-1], high[-1] - 1))
    elif high[-2] > 0:
        last = ".".join(str(part) for part in (*high[:-2], high[-2] - 1)) + ".x"
    else:
        return None
    first = ".".join(str(part) for part in low)
    return first if first == last else f"{first}–{last}"


def pretty_qualifier_range(raw: object) -> str:
    """Pretty-print a qualifier range; unrecognised shapes pass through raw."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text, _, excluded = raw.partition(" excluding ")
    compacted = [_compact_bound_pair(segment.strip()) for segment in text.split("||")]
    if any(segment is None for segment in compacted):
        return raw
    pretty = " · ".join(segment for segment in compacted if isinstance(segment, str))
    if excluded.strip():
        pretty += f" excl. {excluded.strip()}"
    return pretty


def _status_label(status: str, compatibility: dict) -> str:
    """One bounded label per status; the qualified range is compacted, never raw."""
    pretty = _clip_line(
        pretty_qualifier_range(compatibility.get("qualified_range")),
        REGIE_DASHBOARD_COMPAT_RANGE_MAX,
    )
    if status == "native-compatible":
        return f"qualified {pretty}" if pretty else "qualified"
    if status == "outside-qualified-range":
        return f"upgrade to {pretty}" if pretty else "outside qualified range"
    if status == "legacy-only":
        return pretty or "legacy"
    reason = compatibility.get("reason")
    if isinstance(reason, str) and reason.strip():
        clipped = _clip_line(reason.strip(), REGIE_DASHBOARD_COMPAT_REASON_MAX)
        return f"unknown ({clipped})"
    return "compatibility unknown"


def harness_availability_content(rows: list[dict] | None) -> Content:
    """Render one aligned status line per harness: mark, name, version, then label."""
    source = [] if rows is None else rows
    entries: list[tuple[str, str, str]] = []
    for row in source:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        compatibility = row.get("native_compatibility")
        compatibility = compatibility if isinstance(compatibility, dict) else {}
        advertised = row.get("available")
        available = (
            advertised
            if isinstance(advertised, bool)
            else bool(row.get("installed", True)) and not row.get("error")
        )
        beta = " β" if name == "pi" else ""
        if not available:
            pretty = _clip_line(
                pretty_qualifier_range(compatibility.get("qualified_range")),
                REGIE_DASHBOARD_COMPAT_RANGE_MAX,
            )
            error = row.get("error")
            if pretty:
                label = f"install {pretty}"
            elif isinstance(error, str) and error:
                label = error
            else:
                label = "not installed"
            left = f"{REGIE_DASHBOARD_HARNESS_UNAVAILABLE_GLYPH} {name}{beta}"
            entries.append((left, label, REGIE_DASHBOARD_HARNESS_UNAVAILABLE_STYLE))
            continue
        status = compatibility.get("status")
        if not isinstance(status, str) or status not in _COMPATIBILITY_STATUS_STYLES:
            status = "unknown"
        style = _COMPATIBILITY_STATUS_STYLES[status]
        version = compatibility.get("installed_version")
        left = f"{_STATUS_GLYPHS[status]} {name}{beta}"
        if isinstance(version, str) and version:
            left += f" {version}"
        entries.append((left, _status_label(status, compatibility), style))
    width = max((len(left) for left, _, _ in entries), default=0)
    parts: list[StyledPart] = []
    for index, (left, label, style) in enumerate(entries):
        if index:
            parts.append("\n")
        parts.append((left.ljust(width), style))
        parts.append((f"  {label}", style))
    return Content.assemble(*parts) if parts else Content.assemble("")
