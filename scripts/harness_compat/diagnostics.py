"""Bound and clean diagnostic text before it reaches evidence or reports."""

from __future__ import annotations

import re

_ANSI = re.compile(r"(?:\x1b|#x1B|\\x1b)\[[0-?]*[ -/]*[@-~]", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# A named error such as "SyntaxError: ..." (a bare AssertionError only wraps the real cause).
_CAUSE = re.compile(r"\b(?!AssertionError\b)[A-Z]\w*(?:Error|Exception)\b:.{0,180}")


def clean_diagnostic(value: str) -> str:
    """Remove ANSI/control bytes and collapse whitespace for one-line display."""
    return " ".join(_CONTROL.sub("", _ANSI.sub("", value)).split())


def clip_diagnostic(value: str, limit: int, *, tail: bool = False) -> str:
    """Clip cleaned text, retaining the end when it contains the failure cause."""
    cleaned = clean_diagnostic(value)
    if len(cleaned) <= limit:
        return cleaned
    if not tail:
        return cleaned[: limit - 1] + "…"
    cause = _cause(cleaned)
    if cause and len(cause) < limit // 2:
        return f"{cause} … {cleaned[-(limit - len(cause) - 4) :]}"
    return "…" + cleaned[-limit + 1 :]


def _cause(cleaned: str) -> str:
    """The first named error (cut before its stack frames), which a tail clip would lose."""
    found = _CAUSE.search(cleaned)
    if found is None:
        return ""
    text = found.group(0)
    return text[: text.find(" at ")] if " at " in text else text
