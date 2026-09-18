"""Stable-ID participant tree projection for the independent Régie UI."""

from __future__ import annotations

from collections.abc import Iterable
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


def rows_for_projection(
    projection: StateProjection,
    *,
    participant_detail: str,
    cwd_segments: int,
) -> tuple[TreeRow, ...]:
    """Order active public participants by current visible lineage without manufacturing parents."""
    participants = projection.participants
    children: dict[str | None, list[Participant]] = {None: []}
    for participant in participants.values():
        parent_id = participant.parent_id if participant.parent_id in participants else None
        children.setdefault(parent_id, []).append(participant)
    for values in children.values():
        values.sort(key=lambda item: item.participant_id)
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
    for participant_id in sorted(participants):
        participant = participants[participant_id]
        visit(participant, 0)
    return tuple(rows)


def render_tree(rows: Iterable[TreeRow]) -> str:
    return "\n".join(f"{'  ' * row.depth}{row.label}" for row in rows)


__all__ = ["TreeRow", "render_tree", "rows_for_projection"]
