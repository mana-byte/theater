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
    REGIE_DASHBOARD_HARNESS_VERSION_STYLE,
    REGIE_DASHBOARD_ROUTE_LABEL_STYLE,
    REGIE_DASHBOARD_ROUTE_NATIVE_STYLE,
    REGIE_DASHBOARD_ROUTE_SEPARATOR,
    REGIE_DASHBOARD_ROUTE_TMUX_STYLE,
    REGIE_DASHBOARD_ROUTE_UNAVAILABLE_STYLE,
    REGIE_DASHBOARD_ROUTES_INDENT,
    REGIE_DASHBOARD_SENTENCES,
    REGIE_DASHBOARD_TIP_CURSOR_STYLE,
    REGIE_DASHBOARD_TIP_HEADING_STYLE,
    REGIE_DASHBOARD_TIP_HIGHLIGHT_STYLE,
    REGIE_DASHBOARD_TIP_STYLE,
)

_COMPATIBILITY_STATUS_STYLES = {
    "native-compatible": "$success dim",
    "outside-qualified-range": "$warning",
    "legacy-only": "$text-muted",
    "unknown": "$warning dim",
}

_COMPATIBILITY_DETAIL_STYLES = {
    "native-compatible": "$success dim",
    "outside-qualified-range": "$warning dim",
    "unknown": "$warning dim",
}

_ROUTE_KEYS = ("send", "steer", "interrupt")

_BOUND_PAIR = re.compile(r"^>=(\d[\d.]*),<(\d[\d.]*)$")


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
    """Compact one `>=A,<B` pair when B is the next patch or minor bump of A."""
    match = _BOUND_PAIR.match(segment.replace(" ", ""))
    if match is None:
        return None
    low, high = _version_tuple(match.group(1)), _version_tuple(match.group(2))
    if low is None or high is None or len(low) != len(high) or len(low) < 2:
        return None
    if low[:-1] == high[:-1] and high[-1] == low[-1] + 1:
        return ".".join(str(part) for part in low)
    if low[:-2] == high[:-2] and high[-2] == low[-2] + 1 and high[-1] == 0:
        stem = ".".join(str(part) for part in low)
        return f"{stem}–{'.'.join(str(part) for part in low[:-1])}.x"
    return None


def pretty_qualifier_range(raw: object) -> str:
    """Pretty-print a qualifier range; unrecognised shapes pass through raw."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    compacted = [_compact_bound_pair(segment.strip()) for segment in raw.split("||")]
    if any(segment is None for segment in compacted):
        return raw
    return " · ".join(segment for segment in compacted if isinstance(segment, str))


def _compatibility_detail(compatibility: dict) -> str:
    status = compatibility.get("status")
    if not isinstance(status, str) or status not in _COMPATIBILITY_STATUS_STYLES:
        status = "unknown"
    if status == "unknown":
        detail = "compatibility unknown"
        reason = compatibility.get("reason")
        if isinstance(reason, str) and reason.strip():
            clipped = _clip_line(reason.strip(), REGIE_DASHBOARD_COMPAT_REASON_MAX)
            detail = f"{detail} ({clipped})"
        return detail
    pretty = pretty_qualifier_range(compatibility.get("qualified_range"))
    if not pretty:
        return ""
    clipped = _clip_line(pretty, REGIE_DASHBOARD_COMPAT_RANGE_MAX)
    if status == "outside-qualified-range":
        return f"native needs {clipped}"
    if status == "native-compatible":
        return f"qualified {clipped}"
    return ""


def _route_word(value: object) -> StyledPart:
    if value == "native":
        return ("native", REGIE_DASHBOARD_ROUTE_NATIVE_STYLE)
    if value == "tmux":
        return ("tmux", REGIE_DASHBOARD_ROUTE_TMUX_STYLE)
    return ("unavailable", REGIE_DASHBOARD_ROUTE_UNAVAILABLE_STYLE)


def _route_line_parts(routes: dict) -> list[StyledPart]:
    """Indented per-control route line; missing or malformed routes read unavailable."""
    parts: list[StyledPart] = ["\n", REGIE_DASHBOARD_ROUTES_INDENT]
    for index, key in enumerate(_ROUTE_KEYS):
        if index:
            parts.append(REGIE_DASHBOARD_ROUTE_SEPARATOR)
        parts.append((f"{key} ", REGIE_DASHBOARD_ROUTE_LABEL_STYLE))
        parts.append(_route_word(routes.get(key)))
    if routes.get("settings_update") == "native":
        parts.append(REGIE_DASHBOARD_ROUTE_SEPARATOR)
        parts.append(("settings ", REGIE_DASHBOARD_ROUTE_LABEL_STYLE))
        parts.append(("native", REGIE_DASHBOARD_ROUTE_NATIVE_STYLE))
    return parts


def _compatibility_parts(compatibility: dict) -> list[StyledPart]:
    """One `— wiring · detail` suffix plus the indented routes line."""
    status = compatibility.get("status")
    if not isinstance(status, str) or status not in _COMPATIBILITY_STATUS_STYLES:
        status = "unknown"
    wiring = compatibility.get("wiring")
    if wiring not in ("native", "legacy"):
        wiring = "native" if status == "native-compatible" else "legacy"
    parts: list[StyledPart] = []
    version = compatibility.get("installed_version")
    if status != "unknown" and isinstance(version, str) and version:
        parts.append((f" {version}", REGIE_DASHBOARD_HARNESS_VERSION_STYLE))
    parts.append((f" — {wiring}", _COMPATIBILITY_STATUS_STYLES[status]))
    detail = _compatibility_detail(compatibility)
    if detail:
        parts.append((f" · {detail}", _COMPATIBILITY_DETAIL_STYLES[status]))
    routes = compatibility.get("routes")
    if isinstance(routes, dict):
        parts.extend(_route_line_parts(routes))
    return parts


def harness_availability_content(rows: list[dict] | None) -> Content:
    """Render one compact availability line per plugged-in harness."""
    source = [] if rows is None else rows
    parts: list[StyledPart] = []
    for row in source:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if parts:
            parts.append("\n")
        advertised = row.get("available")
        available = (
            advertised
            if isinstance(advertised, bool)
            else bool(row.get("installed", True)) and not row.get("error")
        )
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
        if name == "pi":
            parts.append((" β", "$warning dim"))
        compatibility = row.get("native_compatibility")
        if available and isinstance(compatibility, dict):
            parts.extend(_compatibility_parts(compatibility))
        error = row.get("error")
        if not available and isinstance(error, str) and error:
            parts.append((f" — {error}", "$warning dim"))
    return Content.assemble(*parts) if parts else Content.assemble("")
