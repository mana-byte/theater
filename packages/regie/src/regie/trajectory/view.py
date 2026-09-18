"""Rendered public trajectory surface with stable record navigation."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from regie.trajectory.controller import TrajectoryState
from regie.trajectory.navigation import TrajectoryNavigation
from regie.trajectory.projection import TrajectoryRow, rows_for_state
from regie.trajectory.render import render_rows
from regie.trajectory.search import matching_rows
from regie.trajectory.widgets import TrajectoryFooter


class TrajectoryView(Vertical):
    """A public-record ledger; raw controller state remains available for additive fields."""

    DEFAULT_CSS = """
    TrajectoryView { width: 1fr; height: 1fr; padding: 1 2; }
    #trajectory-ledger { height: 1fr; }
    """

    def __init__(
        self,
        *children: Static,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
        markup: bool = True,
    ) -> None:
        super().__init__(
            *children,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
            markup=markup,
        )
        self._state: TrajectoryState | None = None
        self._rows: tuple[TrajectoryRow, ...] = ()
        self._navigation = TrajectoryNavigation()
        self._query = ""

    @property
    def participant_id(self) -> str | None:
        return self._state.participant_id if self._state is not None else None

    @property
    def selected_record_id(self) -> str | None:
        return self._navigation.selected_id

    def compose(self) -> ComposeResult:
        yield Static("Trajectory", id="trajectory-title")
        yield Static("No trajectory selected", id="trajectory-ledger")
        yield TrajectoryFooter(id="trajectory-footer")

    def show_state(self, state: TrajectoryState) -> None:
        self._state = state
        self._rows = rows_for_state(state)
        visible = matching_rows(self._rows, self._query)
        self._navigation.reconcile(tuple(row.record_id for row in visible))
        title = f"Trajectory · {state.participant_id}"
        if state.stale:
            title += " · stale"
        self.query_one("#trajectory-title", Static).update(title)
        self.query_one("#trajectory-ledger", Static).update(
            render_rows(visible, self._navigation.selected_id)
        )

    def move(self, offset: int) -> str | None:
        visible = matching_rows(self._rows, self._query)
        selected = self._navigation.move(tuple(row.record_id for row in visible), offset)
        if self._state is not None:
            self.show_state(self._state)
        return selected

    def search(self, query: str) -> None:
        self._query = query
        if self._state is not None:
            self.show_state(self._state)


__all__ = ["TrajectoryView"]
