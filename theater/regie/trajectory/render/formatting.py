"""Dependency-light formatting shared by trajectory presentation modules."""

from __future__ import annotations

from theater.trajectory import Timing, TimingProvenance, TrajectoryStatus
from theater.trajectory import sanitize_text as _sanitize_text


def sanitize_text(value: str) -> str:
    """Make terminal controls visible while preserving literal brackets and slashes."""
    return _sanitize_text(value)


def plain_text(value: str) -> str:
    """Return safe plain text for bounded copy."""
    return sanitize_text(value)


def status_label(status: TrajectoryStatus) -> str:
    return status.value.replace("_", " ")


def format_duration(timing: Timing | None) -> str:
    if timing is None or timing.duration_ms is None:
        return "—"
    prefix = "~" if timing.provenance is TimingProvenance.OBSERVED else ""
    return f"{prefix}{format_milliseconds(timing.duration_ms)}"


def format_milliseconds(milliseconds: float) -> str:
    if milliseconds < 1_000:
        return f"{milliseconds:g}ms"
    seconds = milliseconds / 1_000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, remainder = divmod(seconds, 60)
    return f"{int(minutes)}m {remainder:.0f}s"


__all__ = [
    "format_duration",
    "format_milliseconds",
    "plain_text",
    "sanitize_text",
    "status_label",
]
