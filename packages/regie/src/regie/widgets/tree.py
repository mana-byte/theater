"""Compact public-state participant tree widget."""

from __future__ import annotations

from collections.abc import Mapping

from textual.widgets import Static

from regie.tree import rows_for_projection
from theater.frontend import StateProjection


class ParticipantTree(Static):
    """Render stable-ID public projections without a private participant-tree RPC."""

    def show_projection(
        self,
        projection: StateProjection,
        *,
        participant_detail: str = "cwd",
        cwd_segments: int = 2,
        stage_reasons: Mapping[str, str] | None = None,
    ) -> None:
        prefix = "stale — reconnecting\n" if projection.stale else ""
        lines = [prefix] if prefix else []
        rows = rows_for_projection(
            projection,
            participant_detail=participant_detail,
            cwd_segments=cwd_segments,
        )
        for row in rows:
            suffix = f" · {row.detail}" if row.detail else ""
            stage_reason = (stage_reasons or {}).get(row.participant_id)
            if stage_reason:
                suffix += f" · {stage_reason}"
            route = "route" if row.addressable else "no route"
            lines.append(f"{'  ' * row.depth}{row.label} [{row.status}; {route}]{suffix}")
        self.update("\n".join(lines) if lines else "No active participants")


__all__ = ["ParticipantTree"]
