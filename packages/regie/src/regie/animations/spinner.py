"""A deliberately tiny idle-free status spinner."""

from __future__ import annotations

_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


def spinner_frame(index: int) -> str:
    """Return a deterministic frame without allocating a background timer."""
    return _FRAMES[index % len(_FRAMES)]


__all__ = ["spinner_frame"]
