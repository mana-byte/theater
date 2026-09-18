"""Bounded local filtering of already-public trajectory rows."""

from __future__ import annotations

from collections.abc import Iterable

from regie.trajectory.projection import TrajectoryRow


def matching_rows(rows: Iterable[TrajectoryRow], query: str) -> tuple[TrajectoryRow, ...]:
    """Filter a displayed page; deeper search remains the public API controller's job."""
    needle = query.strip().casefold()
    return tuple(
        row
        for row in rows
        if not needle or needle in row.kind.casefold() or needle in row.summary.casefold()
    )


__all__ = ["matching_rows"]
