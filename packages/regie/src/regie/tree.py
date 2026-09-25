"""Stable-ID participant tree projection for the independent Régie UI."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from regie.formatting import participant_label, shorten_path
from theater.frontend import Participant, StateProjection


@dataclass(frozen=True, slots=True)
class TreeRow:
    participant_id: str
    depth: int
    label: str
    detail: str
    status: str
    addressable: bool


def _participant_node(
    participant: Participant,
    *,
    harness_icons: Mapping[str, str] | None = None,
    participant_costs: Mapping[str, int] | None = None,
) -> dict[str, object]:
    route = participant.terminal_route
    terminal_id = route.identity.terminal_id if route is not None else None
    cost = (participant_costs or {}).get(participant.participant_id)
    return {
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


def tree_for_projection(
    projection: StateProjection,
    *,
    harness_icons: Mapping[str, str] | None = None,
    participant_costs: Mapping[str, int] | None = None,
    layout: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """Adapt public participants to the presentation renderer's nested forest."""
    participants = projection.participants
    nodes = {
        participant_id: _participant_node(
            item,
            harness_icons=harness_icons,
            participant_costs=participant_costs,
        )
        for participant_id, item in participants.items()
    }
    insertion_order = {participant_id: index for index, participant_id in enumerate(participants)}
    ordered_ids = sorted(
        participants,
        key=lambda participant_id: (
            participants[participant_id].created_at is None,
            participants[participant_id].created_at or 0.0,
            insertion_order[participant_id],
        ),
    )
    children: dict[str, list[str]] = {}
    root_ids: list[str] = []
    for participant_id in ordered_ids:
        parent_id = participants[participant_id].parent_id
        if parent_id in nodes and parent_id != participant_id:
            children.setdefault(parent_id, []).append(participant_id)
        else:
            root_ids.append(participant_id)

    raw_orders = (layout or {}).get("orders", {})
    orders = raw_orders if isinstance(raw_orders, Mapping) else {}

    def ordered(parent_id: str | None, participant_ids: list[str]) -> list[str]:
        stored = orders.get(parent_id or "", ())
        if not isinstance(stored, list | tuple):
            stored = ()
        available = set(participant_ids)
        result = [item for item in stored if isinstance(item, str) and item in available]
        present = set(result)
        result.extend(item for item in participant_ids if item not in present)
        return result

    visited: set[str] = set()

    def build(participant_id: str, ancestry: frozenset[str]) -> dict[str, object]:
        visited.add(participant_id)
        node = dict(nodes[participant_id])
        node["children"] = [
            build(child_id, ancestry | {participant_id})
            for child_id in ordered(participant_id, children.get(participant_id, []))
            if child_id not in ancestry and child_id not in visited
        ]
        return node

    roots: list[dict[str, object]] = []
    for participant_id in (*ordered(None, root_ids), *ordered_ids):
        if participant_id not in visited:
            roots.append(build(participant_id, frozenset()))
    return roots


def rows_for_projection(
    projection: StateProjection,
    *,
    participant_detail: str,
    cwd_segments: int,
) -> tuple[TreeRow, ...]:
    """Order active public participants by current visible lineage without manufacturing parents."""
    participants = projection.participants
    insertion_order = {participant_id: index for index, participant_id in enumerate(participants)}
    children: dict[str | None, list[Participant]] = {None: []}
    for participant in participants.values():
        parent_id = participant.parent_id if participant.parent_id in participants else None
        children.setdefault(parent_id, []).append(participant)
    for values in children.values():
        values.sort(
            key=lambda item: (
                item.created_at is None,
                item.created_at if item.created_at is not None else 0.0,
                insertion_order[item.participant_id],
            )
        )
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
        for child in children.get(participant.participant_id, []):
            visit(child, depth + 1)

    for root in children[None]:
        visit(root, 0)
    for participant_id in participants:
        participant = participants[participant_id]
        visit(participant, 0)
    return tuple(rows)


def render_tree(rows: Iterable[TreeRow]) -> str:
    return "\n".join(f"{'  ' * row.depth}{row.label}" for row in rows)


__all__ = [
    "TreeRow",
    "render_tree",
    "rows_for_projection",
    "tree_for_projection",
]
