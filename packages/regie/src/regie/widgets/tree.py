"""Compact public-state participant tree widget."""

from __future__ import annotations

from collections.abc import Mapping

from textual.widgets import Static

from regie.tree import TreeRow, rows_for_projection
from regie.widgets.leaf import render_leaf
from theater.frontend import StateProjection


class ParticipantTree(Static):
    """Render stable-ID public projections without a private participant-tree RPC."""

    def __init__(
        self,
        content: str = "",
        *,
        expand: bool = False,
        shrink: bool = False,
        markup: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(
            content,
            expand=expand,
            shrink=shrink,
            markup=markup,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self._rows: tuple[TreeRow, ...] = ()
        self._selected_id: str | None = None
        self._stage_reasons: Mapping[str, str] = {}
        self._staged_id: str | None = None
        self._trajectory_id: str | None = None
        self._stale = False

    @property
    def selected_id(self) -> str | None:
        return self._selected_id

    @property
    def participant_ids(self) -> tuple[str, ...]:
        return tuple(row.participant_id for row in self._rows)

    def show_projection(
        self,
        projection: StateProjection,
        *,
        participant_detail: str = "cwd",
        cwd_segments: int = 2,
        stage_reasons: Mapping[str, str] | None = None,
        selected_id: str | None = None,
        staged_id: str | None = None,
        trajectory_id: str | None = None,
    ) -> str | None:
        self._rows = rows_for_projection(
            projection,
            participant_detail=participant_detail,
            cwd_segments=cwd_segments,
        )
        ids = self.participant_ids
        preferred = selected_id if selected_id in ids else self._selected_id
        self._selected_id = preferred if preferred in ids else (ids[0] if ids else None)
        self._stage_reasons = dict(stage_reasons or {})
        self._staged_id = staged_id
        self._trajectory_id = trajectory_id
        self._stale = projection.stale
        self._render_rows()
        return self._selected_id

    def select(self, participant_id: str | None) -> str | None:
        if participant_id in self.participant_ids:
            self._selected_id = participant_id
            self._render_rows()
        return self._selected_id

    def move(self, offset: int) -> str | None:
        ids = self.participant_ids
        if not ids:
            self._selected_id = None
            return None
        try:
            index = ids.index(self._selected_id) if self._selected_id is not None else 0
        except ValueError:
            index = 0
        self._selected_id = ids[max(0, min(len(ids) - 1, index + offset))]
        self._render_rows()
        return self._selected_id

    def mark_surfaces(self, *, staged_id: str | None, trajectory_id: str | None) -> None:
        self._staged_id = staged_id
        self._trajectory_id = trajectory_id
        self._render_rows()

    def _render_rows(self) -> None:
        prefix = ["stale — reconnecting"] if self._stale else []
        lines = prefix + [
            render_leaf(
                row,
                selected=row.participant_id == self._selected_id,
                staged=row.participant_id == self._staged_id,
                trajectory=row.participant_id == self._trajectory_id,
                stage_reason=self._stage_reasons.get(row.participant_id),
            )
            for row in self._rows
        ]
        self.update("\n".join(lines) if lines else "No active participants — Ctrl+P to spawn")


__all__ = ["ParticipantTree"]
