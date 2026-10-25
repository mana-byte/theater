"""Stable-ID participant tree projection for the independent Régie UI."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TypeGuard, cast

from regie.formatting import participant_label, shorten_path
from regie.marks import MarkKind, RowMark
from regie.tree_layout import TreeLayout
from regie.tree_nodes import ParticipantNode, PendingSpawnNode, SeparatorNode, TreeNode
from theater.frontend import Participant, StateProjection


@dataclass(frozen=True, slots=True)
class TreeRow:
    participant_id: str
    depth: int
    label: str
    detail: str
    status: str
    addressable: bool


@dataclass(frozen=True, slots=True)
class ParticipantGroups:
    ordered_ids: tuple[str, ...]
    siblings: dict[str | None, list[str]]


def participant_groups(projection: StateProjection) -> ParticipantGroups:
    """Group participants by visible parent in their stable default order."""
    participants = projection.participants
    insertion = {participant_id: index for index, participant_id in enumerate(participants)}
    ordered_ids = tuple(
        sorted(
            participants,
            key=lambda participant_id: (
                participants[participant_id].created_at is None,
                participants[participant_id].created_at or 0.0,
                insertion[participant_id],
            ),
        )
    )
    siblings: dict[str | None, list[str]] = {None: []}
    for participant_id in ordered_ids:
        participant = participants[participant_id]
        parent_id = participant.parent_id
        if parent_id not in participants or parent_id == participant_id:
            parent_id = None
        siblings.setdefault(parent_id, []).append(participant_id)
    return ParticipantGroups(ordered_ids, siblings)


def _participant_node(
    participant: Participant,
    *,
    harness_icons: Mapping[str, str] | None = None,
    participant_costs: Mapping[str, int] | None = None,
    mark: RowMark | None = None,
) -> ParticipantNode:
    route = participant.terminal_route
    terminal_id = route.identity.terminal_id if route is not None else None
    cost = (participant_costs or {}).get(participant.participant_id)
    node: ParticipantNode = {
        "id": participant.participant_id,
        "parent_id": participant.parent_id,
        "tier": participant.origin,
        "harness": participant.harness,
        "icon": (harness_icons or {}).get(participant.harness),
        "status": participant.status,
        "cwd": participant.cwd,
        "name": participant.name,
        "description": participant.description,
        "addressable": participant.addressable,
        "human_presence": {"state": participant.presence},
        "trusted_identity": participant.trusted_identity,
        "transcript_identity": (
            participant.transcript_identity.to_wire()
            if participant.transcript_identity is not None
            else None
        ),
        "tmux_pane": terminal_id,
        "usage_cost_microcents": cost,
        "children": [],
    }
    if mark is not None:
        node["mark"] = mark
    return node


def _separator_node(separator_id: str, name: str, *, collapsed: bool) -> SeparatorNode:
    return {
        "id": separator_id,
        "kind": "separator",
        "name": name,
        "collapsed": collapsed,
        "count": 0,
        "children": [],
    }


def _agent_count(node: TreeNode) -> int:
    return 1 + sum(_agent_count(child) for child in node["children"])


def _agents(node: TreeNode) -> list[TreeNode]:
    return [node, *(agent for child in node["children"] for agent in _agents(child))]


#: The status a folded heading shows for its hidden agents, strongest first.
_FOLD_STATUSES = ("awaiting_input", "dead", "working")

#: A hidden agent whose operation failed or is in doubt shows through as the status it reads as.
_MARK_FOLD_STATUS = {
    MarkKind.BLOCKED: "awaiting_input",
    MarkKind.UNCERTAIN: "awaiting_input",
    MarkKind.FAILED: "dead",
}


def _fold_status(agents: list[TreeNode]) -> str | None:
    """Needing a human outranks a dead agent, which outranks working; an all-idle fold is blank."""
    statuses = {agent.get("status") for agent in agents}
    for agent in agents:
        mark = agent.get("mark")
        if isinstance(mark, RowMark) and mark.kind in _MARK_FOLD_STATUS:
            statuses.add(_MARK_FOLD_STATUS[mark.kind])
    return next((status for status in _FOLD_STATUSES if status in statuses), None)


def _is_separator(node: TreeNode) -> TypeGuard[SeparatorNode]:
    return node.get("kind") == "separator"


def _descendants(nodes: list[TreeNode]) -> list[TreeNode]:
    """Every participant below *nodes*, including those a folded section already hides."""
    found: list[TreeNode] = []
    for node in nodes:
        if _is_separator(node):
            found.extend(node.get("folded_agents", []))  # already flat: each with its subtree
        else:
            found.append(node)
            found.extend(_descendants(node["children"]))
    return found


def _sections(built: list[TreeNode]) -> list[TreeNode]:
    """Count each separator's agents; a collapsed one folds them away and records their ids."""
    visible: list[TreeNode] = []
    heading: SeparatorNode | None = None
    count = 0
    for node in built:
        if _is_separator(node):
            heading, count = node, 0
            visible.append(node)
            continue
        if heading is not None:
            count += _agent_count(node)
            heading["count"] = count
            if heading["collapsed"]:
                hidden = [*heading.get("folded_agents", []), *_agents(node)]
                heading["folded_agents"] = hidden
                heading["folded"] = [agent["id"] for agent in hidden]
                heading["folded_status"] = _fold_status(hidden)
                continue
        visible.append(node)
    return visible


def tree_for_projection(
    projection: StateProjection,
    *,
    harness_icons: Mapping[str, str] | None = None,
    participant_costs: Mapping[str, int] | None = None,
    layout: TreeLayout | None = None,
    pending_spawn: Mapping[str, object] | None = None,
    marks: Mapping[str, RowMark] | None = None,
) -> list[TreeNode]:
    """Adapt public participants to the presentation renderer's nested forest.

    A pending spawn is drawn last among the roots, where Theater places a new agent.
    """
    participants = projection.participants
    nodes = {
        participant_id: _participant_node(
            item,
            harness_icons=harness_icons,
            participant_costs=participant_costs,
            mark=(marks or {}).get(participant_id),
        )
        for participant_id, item in participants.items()
    }
    groups = participant_groups(projection)
    active_layout = layout or TreeLayout()

    visited: set[str] = set()

    def build(participant_id: str, ancestry: frozenset[str]) -> ParticipantNode:
        visited.add(participant_id)
        node = nodes[participant_id].copy()
        node["children"] = build_siblings(
            participant_id,
            groups.siblings.get(participant_id, []),
            ancestry | {participant_id},
        )
        if participant_id in active_layout.folded and node["children"]:
            hidden = _descendants(node["children"])
            node["hidden_count"] = len(hidden)
            node["hidden_ids"] = [agent["id"] for agent in hidden]
            node["hidden_status"] = _fold_status(hidden)
            node["children"] = []
        return node

    def build_siblings(
        parent_id: str | None,
        participant_ids: list[str],
        ancestry: frozenset[str],
    ) -> list[TreeNode]:
        built: list[TreeNode] = []
        for item_id in active_layout.ordered(parent_id, participant_ids):
            if item_id in nodes:
                if item_id not in ancestry and item_id not in visited:
                    built.append(build(item_id, ancestry))
                continue
            record = active_layout.separators.get(item_id)
            name = record.get("name") if record is not None else None
            if isinstance(name, str) and (name or item_id == active_layout.pending):
                collapsed = bool(record and record.get("collapsed"))
                built.append(_separator_node(item_id, name, collapsed=collapsed))
        return _sections(built)

    roots = build_siblings(None, groups.siblings[None], frozenset())
    for participant_id in groups.ordered_ids:
        if participant_id not in visited:
            roots.append(build(participant_id, frozenset()))
    if pending_spawn is not None:
        roots.append(
            cast(PendingSpawnNode, {**pending_spawn, "id": "new", "kind": "spawn", "children": []})
        )
    return roots


def rows_for_projection(
    projection: StateProjection,
    *,
    participant_detail: str,
    cwd_segments: int,
) -> tuple[TreeRow, ...]:
    """Order active public participants by current visible lineage without manufacturing parents."""
    participants = projection.participants
    groups = participant_groups(projection)
    rows: list[TreeRow] = []
    visited: set[str] = set()

    def visit(participant: Participant, depth: int) -> None:
        if participant.participant_id in visited:
            return
        visited.add(participant.participant_id)
        detail = (
            participant.description
            if participant_detail == "description"
            else shorten_path(participant.cwd, segments=cwd_segments)
        )
        rows.append(
            TreeRow(
                participant.participant_id,
                depth,
                participant_label(participant),
                detail or "",
                participant.status,
                participant.addressable,
            )
        )
        for child_id in groups.siblings.get(participant.participant_id, []):
            visit(participants[child_id], depth + 1)

    for root_id in groups.siblings[None]:
        visit(participants[root_id], 0)
    for participant_id in groups.ordered_ids:
        visit(participants[participant_id], 0)
    return tuple(rows)


def render_tree(rows: Iterable[TreeRow]) -> str:
    return "\n".join(f"{'  ' * row.depth}{row.label}" for row in rows)


__all__ = [
    "TreeRow",
    "participant_groups",
    "render_tree",
    "rows_for_projection",
    "tree_for_projection",
]
