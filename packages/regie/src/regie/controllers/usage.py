"""Presentation adapter for the public usage facade."""

from __future__ import annotations

from regie.usage import UsageSnapshot


def usage_status(snapshot: UsageSnapshot) -> str:
    """Render totals without interpreting accounting values locally."""
    return (
        " · ".join(f"{name}={value}" for name, value in snapshot.totals.items())
        or "usage unavailable"
    )


__all__ = ["usage_status"]
