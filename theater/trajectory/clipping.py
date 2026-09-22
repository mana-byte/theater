"""UTF-8-safe previews retaining only the source text needed for later clipping."""

from __future__ import annotations

import re

from theater.trajectory.enums import TrajectoryValidationError

_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_ENCODING_CHUNK_CHARS = 64 * 1024
type RetainedSource = tuple[str, str]


def encoded_size(value: str) -> int:
    """Validate the entire input without allocating its entire UTF-8 encoding."""
    if not isinstance(value, str):
        raise TrajectoryValidationError("trajectory text must be a string")
    if value.isascii():
        return len(value)
    try:
        return sum(
            len(value[start : start + _ENCODING_CHUNK_CHARS].encode("utf-8"))
            for start in range(0, len(value), _ENCODING_CHUNK_CHARS)
        )
    except UnicodeEncodeError as exc:
        raise TrajectoryValidationError("trajectory text must contain valid UTF-8") from exc


def display_text(value: str) -> str:
    return _CONTROLS.sub(lambda match: f"\\x{ord(match[0]):02x}", value)


def control_safe(value: str) -> bool:
    return _CONTROLS.search(value) is None


def sanitize_text(value: str) -> str:
    """Validate UTF-8 and make terminal controls visible without Rich escaping."""
    encoded_size(value)
    return display_text(value)


def _take(value: str, budget: int, *, suffix: bool = False, start: int = 0) -> tuple[str, str]:
    # Sanitization never shrinks text, so only this bounded source window can fit.
    candidate = value[max(start, len(value) - budget) :] if suffix else value[:budget]
    displayed = display_text(candidate)
    if len(displayed.encode("utf-8")) <= budget:
        return candidate, displayed
    used = 0
    count = 0
    for char in reversed(candidate) if suffix else candidate:
        width = len(display_text(char).encode("utf-8"))
        if used + width > budget:
            break
        used += width
        count += 1
    selected = candidate[len(candidate) - count :] if suffix else candidate[:count]
    return selected, display_text(selected)


def clip_text(
    value: str, max_bytes: int, prior_omitted: int = 0
) -> tuple[str, int, RetainedSource]:
    """Clip original source, excluding synthetic markers from omission accounting."""
    total = prior_omitted + encoded_size(value)
    if prior_omitted == 0 and total <= max_bytes:
        displayed = display_text(value)
        if len(displayed.encode("utf-8")) <= max_bytes:
            return displayed, 0, (value, "")
    marker = f"… {total} bytes omitted …"
    for _ in range(12):
        available = max_bytes - len(marker.encode("utf-8"))
        if available <= 0:
            return "", total, ("", "")
        head_source, head = _take(value, available // 2)
        tail_source, tail = _take(
            value, available - len(head.encode("utf-8")), suffix=True, start=len(head_source)
        )
        omitted = total - encoded_size(head_source) - encoded_size(tail_source)
        next_marker = f"… {omitted} bytes omitted …"
        result = head + next_marker + tail
        if next_marker == marker and len(result.encode("utf-8")) <= max_bytes:
            return result, omitted, (head_source, tail_source)
        marker = next_marker
    marker = f"… {total} bytes omitted …"
    return (marker if len(marker.encode("utf-8")) <= max_bytes else ""), total, ("", "")
