"""Text rendering for a bounded trajectory ledger."""

from __future__ import annotations

from collections.abc import Iterable

from regie.trajectory.projection import TrajectoryRow


def render_rows(rows: Iterable[TrajectoryRow], selected_id: str | None) -> str:
    """Render IDs only as local navigation anchors, not as inferred domain state."""
    rendered = [
        f"{'▶' if row.record_id == selected_id else ' '} {row.kind} · {row.summary}" for row in rows
    ]
    return "\n".join(rendered) if rendered else "No trajectory records in this public page"


__all__ = ["render_rows"]
