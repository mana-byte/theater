from __future__ import annotations

from types import MappingProxyType

from regie.tree import rows_for_projection

from theater.frontend import EventCursor, Participant, StateProjection


def _participant(
    participant_id: str, *, parent_id: str | None = None, name: str | None = None
) -> Participant:
    return Participant.from_wire(
        {
            "participant_id": participant_id,
            "origin": "spawned",
            "harness": "codex",
            "status": "idle",
            "owner": {"kind": "local_operator", "revision": 1},
            "parent_id": parent_id,
            "name": name,
            "addressable": False,
            "presence": "unknown",
            "actions": {},
        }
    )


def test_tree_keeps_stable_ids_and_lineage_when_display_names_change() -> None:
    parent = _participant("parent-id", name="renamed parent")
    child = _participant("child-id", parent_id="parent-id", name="renamed child")
    projection = StateProjection(
        cursor=EventCursor("stream-a", 1),
        participants=MappingProxyType({parent.participant_id: parent, child.participant_id: child}),
        operations=MappingProxyType({}),
        jobs=MappingProxyType({}),
        providers=MappingProxyType({}),
        workspaces=MappingProxyType({}),
    )

    rows = rows_for_projection(projection, participant_detail="cwd", cwd_segments=2)

    assert [(row.participant_id, row.depth, row.label) for row in rows] == [
        ("parent-id", 0, "renamed parent"),
        ("child-id", 1, "renamed child"),
    ]
