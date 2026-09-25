"""Stable-ID participant tree projection for the independent Régie UI."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from regie.formatting import participant_label, shorten_path
from regie.tree_layout import TreeLayout
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
    layout: TreeLayout | None = None,
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
    groups = participant_groups(projection)
    active_layout = layout or TreeLayout()

    visited: set[str] = set()

    def build(participant_id: str, ancestry: frozenset[str]) -> dict[str, object]:
        visited.add(participant_id)
        node = dict(nodes[participant_id])
        node["children"] = build_siblings(
            participant_id,
            groups.siblings.get(participant_id, []),
            ancestry | {participant_id},
        )
        return node

    def build_siblings(
        parent_id: str | None,
        participant_ids: list[str],
        ancestry: frozenset[str],
    ) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for item_id in active_layout.ordered(parent_id, participant_ids):
            if item_id in nodes:
                if item_id not in ancestry and item_id not in visited:
                    result.append(build(item_id, ancestry))
                continue
            record = active_layout.separators.get(item_id)
            name = record.get("name") if record is not None else None
            if isinstance(name, str) and name:
                result.append({"id": item_id, "kind": "separator", "name": name, "children": []})
        return result

    roots = build_siblings(None, groups.siblings[None], frozenset())
    for participant_id in groups.ordered_ids:
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
