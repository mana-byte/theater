"""Safe bounded text layout for public, forward-compatible values."""

from __future__ import annotations


def bounded_text(value: object, *, limit: int = 160) -> str:
    """Render an unknown value compactly without allowing it to dominate a pane."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else f"{text[: max(1, limit - 1)]}…"


__all__ = ["bounded_text"]
