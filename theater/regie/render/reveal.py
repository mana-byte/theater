"""Pure clipping for the régie startup typing effect."""

from __future__ import annotations

from collections.abc import Sequence

type StyledPart = str | tuple[str, str]


def clip_parts(parts: Sequence[StyledPart], visible: int) -> list[StyledPart]:
    """Keep the first *visible* codepoints while preserving part styles."""
    remaining = max(0, visible)
    clipped: list[StyledPart] = []
    for part in parts:
        if remaining == 0:
            break
        value = part if isinstance(part, str) else part[0]
        text = value[:remaining]
        if text:
            clipped.append(text if isinstance(part, str) else (text, part[1]))
        remaining -= len(text)
    return clipped
