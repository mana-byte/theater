"""Bound and clean diagnostic text before it reaches evidence or reports."""

from __future__ import annotations

import re

_ANSI = re.compile(r"(?:\x1b|#x1B|\\x1b)\[[0-?]*[ -/]*[@-~]", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean_diagnostic(value: str) -> str:
    """Remove ANSI/control bytes and collapse whitespace for one-line display."""
    return " ".join(_CONTROL.sub("", _ANSI.sub("", value)).split())


def clip_diagnostic(value: str, limit: int, *, tail: bool = False) -> str:
    """Clip cleaned text, retaining the end when it contains the failure cause."""
    cleaned = clean_diagnostic(value)
    if len(cleaned) <= limit:
        return cleaned
    return "…" + cleaned[-limit + 1 :] if tail else cleaned[: limit - 1] + "…"
