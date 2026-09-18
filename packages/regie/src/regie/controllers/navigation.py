"""Selection state keyed only by stable public participant IDs."""

from __future__ import annotations

from collections.abc import Collection


class NavigationState:
    """Keep a selection through display-name changes without inventing a target."""

    def __init__(self) -> None:
        self._selected_id: str | None = None

    @property
    def selected_id(self) -> str | None:
        return self._selected_id

    def select(self, participant_id: str) -> None:
        self._selected_id = participant_id

    def reconcile(self, visible_ids: Collection[str]) -> str | None:
        if self._selected_id not in visible_ids:
            self._selected_id = next(iter(visible_ids), None)
        return self._selected_id


__all__ = ["NavigationState"]
