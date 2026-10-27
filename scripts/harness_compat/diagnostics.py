"""Bound and clean diagnostic text before it reaches evidence or reports."""

from __future__ import annotations

import re

#: Room for the cause of one failure: every producer of a failing check's detail, the end-of-run
#: ledger and the issue excerpts bound it here, so none clips a cause another one kept.
FAILURE_DETAIL_CHARS = 1000
_ANSI = re.compile(r"(?:\x1b|#x1B|\\x1b)\[[0-?]*[ -/]*[@-~]", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# A named error such as "SyntaxError: ..." (a bare AssertionError only wraps the real cause).
_CAUSE = re.compile(r"\b(?!AssertionError\b)[A-Z]\w*(?:Error|Exception)\b:.{0,180}")


def clean_diagnostic(value: str) -> str:
    """Remove ANSI/control bytes and collapse whitespace for one-line display."""
    return " ".join(_CONTROL.sub("", _ANSI.sub("", value)).split())


def exception_detail(error: BaseException) -> str:
    """Keep publication causes visible even when several operations failed."""
    details: list[str] = []
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, BaseExceptionGroup):
            details.append("; ".join(exception_detail(cause) for cause in error.exceptions))
        else:
            details.append(f"{type(error).__name__}: {error}")
        cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        if cause is None:
            break
        error = cause
    return "; preceded by ".join(details)


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
