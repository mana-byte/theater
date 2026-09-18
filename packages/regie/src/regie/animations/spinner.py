"""A deliberately tiny idle-free status spinner."""

from __future__ import annotations

_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
SPINNER_CYCLE = len(_FRAMES)


def spinner_frame(index: int) -> str:
    """Return a deterministic frame without allocating a background timer."""
    return _FRAMES[index % len(_FRAMES)]


def advance_spinner_frame(index: int) -> int:
    return (index + 1) % SPINNER_CYCLE


__all__ = ["SPINNER_CYCLE", "advance_spinner_frame", "spinner_frame"]
