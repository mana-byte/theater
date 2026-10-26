"""The typing-only host that every ParticipantTree mixin extends."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from textual.containers import VerticalScroll
    from textual.timer import Timer
    from textual.widget import Widget

    from regie.marks import MarkKind
    from regie.menu import MenuItem, MenuView
    from regie.motions.retirement import LeafRetirementController
    from regie.motions.reveal import LeafRevealController
    from regie.motions.wipe import LeafWipeController
    from regie.render.layout import Key, RenderedLine, TreeLines
    from regie.widgets.leaf import AgentLeaf

    class TreeHost(VerticalScroll):
        """State set by ``ParticipantTree.__init__`` and methods one mixin calls on another."""

        _lines_data: list[RenderedLine]
        _key_widgets: dict[Key, Widget]
        _overlaid: set[Key]
        _reveals: dict[Key, int]
        _retiring: dict[Key, AgentLeaf]
        _retiring_predecessors: dict[Key, Key | None]
        _animate_new: set[Key]
        _leaf_reveal: LeafRevealController
        _leaf_retirement: LeafRetirementController
        _leaf_reveal_timer: Timer | None
        _leaf_retirement_timer: Timer | None
        _motion: bool
        _wipes: LeafWipeController
        _wipe_timer: Timer | None
        _statuses: dict[Key, str]
        _attention_since: dict[Key, float]
        _attention_phase: int
        _attention_timer: Timer | None
        _mark_kinds: dict[str, MarkKind] | None
        _hidden_owner: dict[Key, Key]
        _kills: set[Key]
        _held: dict[Key, tuple[bool, int]]
        _selected_key: Key | None
        menu_source: Callable[[], Sequence[MenuItem]]
        _menu_view: MenuView
        _menu_return: Key | None
        _menu_hidden: bool

        @property
        def selectable_keys(self) -> tuple[Key, ...]: ...
        def _remove_widget(self, widget: Widget) -> None: ...
        def _reconcile_empty(self) -> None: ...
        def _rerender(self) -> None: ...
        def _render_lines(self, opened: bool) -> TreeLines: ...
        def _show_lines(self, lines: TreeLines) -> None: ...
        def _apply_selection(self) -> None: ...

    _TreeBase = TreeHost
else:
    _TreeBase = object

__all__ = ["_TreeBase"]
