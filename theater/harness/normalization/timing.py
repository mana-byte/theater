"""Shared ISO-8601 timestamp conversion."""

from __future__ import annotations

from datetime import datetime


def iso_epoch(value: object) -> float | None:
    """Return an ISO-8601 timestamp as epoch seconds."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
