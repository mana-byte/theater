"""Rendered participant rows shared by tree interaction tests."""

from __future__ import annotations

from regie.widgets import ParticipantTree
from regie.widgets.leaf import AgentLeaf


def leaf(tree: ParticipantTree, participant_id: str) -> AgentLeaf:
    widget = tree._key_widgets[("p", participant_id)]
    assert isinstance(widget, AgentLeaf)
    return widget


def row(widget: AgentLeaf) -> str:
    return widget.render_line(1).text.rstrip()


def rows(widget: AgentLeaf) -> list[str]:
    return [widget.render_line(index).text.rstrip() for index in range(3)]


def stripped_rows(widget: AgentLeaf) -> list[str]:
    return [line.strip() for line in rows(widget)]
