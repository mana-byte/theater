"""Public helpers for bounded, Rich-safe trajectory detail values."""

from __future__ import annotations

from theater.trajectory.models import ContentPreview, bound_detail_fields, escape_rich_text


def bounded_preview(text: str, *, max_bytes: int | None = None) -> ContentPreview:
    """Return a UTF-8 bounded preview with an exact omitted-byte count."""
    if max_bytes is None:
        return ContentPreview.from_text(text)
    return ContentPreview.from_text(text, max_bytes=max_bytes)


def clip_utf8(text: str, *, max_bytes: int) -> ContentPreview:
    """Alias for callers that name the operation rather than its result."""
    return ContentPreview.from_text(text, max_bytes=max_bytes)


__all__ = [
    "ContentPreview",
    "bound_detail_fields",
    "bounded_preview",
    "clip_utf8",
    "escape_rich_text",
]
