"""Stable record-ID navigation for the independent trajectory surface."""

from __future__ import annotations

from collections.abc import Collection


class TrajectoryNavigation:
    """Preserve a selected record through public delta updates."""

    def __init__(self) -> None:
        self._selected_id: str | None = None

    @property
    def selected_id(self) -> str | None:
        return self._selected_id

    def reconcile(self, record_ids: Collection[str]) -> str | None:
        if self._selected_id not in record_ids:
            self._selected_id = next(iter(record_ids), None)
        return self._selected_id

    def move(self, record_ids: tuple[str, ...], offset: int) -> str | None:
        if not record_ids:
            self._selected_id = None
            return None
        try:
            index = record_ids.index(self._selected_id) if self._selected_id is not None else 0
        except ValueError:
            index = 0
        self._selected_id = record_ids[max(0, min(len(record_ids) - 1, index + offset))]
        return self._selected_id


__all__ = ["TrajectoryNavigation"]
