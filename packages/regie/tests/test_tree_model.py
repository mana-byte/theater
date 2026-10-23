"""The participant tree model and its persisted layout."""

from __future__ import annotations

import json
from types import MappingProxyType

from regie.tree import rows_for_projection, tree_for_projection
from regie.tree_layout import TreeLayout

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


def test_tree_projection_preserves_public_transcript_identity() -> None:
    participant = Participant.from_wire(
        {
            "participant_id": "participant-a",
            "origin": "spawned",
            "harness": "codex",
            "status": "idle",
            "owner": {"kind": "local_operator", "revision": 1},
            "addressable": False,
            "presence": "unknown",
            "actions": {},
            "transcript_identity": {
                "state": "trusted",
                "session_id": "session-a",
                "provenance": "exact",
                "location": "/tmp/transcript.jsonl",
                "domain": None,
                "detail": None,
            },
        }
    )
    projection = StateProjection(
        cursor=EventCursor("stream-a", 1),
        participants=MappingProxyType({participant.participant_id: participant}),
        operations=MappingProxyType({}),
        jobs=MappingProxyType({}),
        providers=MappingProxyType({}),
        workspaces=MappingProxyType({}),
    )

    [node] = tree_for_projection(projection, harness_icons={"codex": "◈"})

    assert node["transcript_identity"] == participant.transcript_identity.to_wire()
    assert node["icon"] == "◈"


def test_internal_nodes_keep_participant_kind_absent_and_pending_extras() -> None:
    participant = _participant("participant-a")
    projection = StateProjection(
        cursor=EventCursor("stream-a", 1),
        participants=MappingProxyType({participant.participant_id: participant}),
        operations=MappingProxyType({}),
        jobs=MappingProxyType({}),
        providers=MappingProxyType({}),
        workspaces=MappingProxyType({}),
    )

    participant_node, pending_node = tree_for_projection(
        projection, pending_spawn={"id": "old", "kind": "other", "cwd": "/tmp", "extra": 17}
    )

    assert "kind" not in participant_node
    assert pending_node == {
        "id": "new",
        "kind": "spawn",
        "cwd": "/tmp",
        "extra": 17,
        "children": [],
    }


def test_tree_layout_round_trips_order_and_preserves_unknown_ids(tmp_path) -> None:
    path = tmp_path / "tree-layout.json"
    layout = TreeLayout(
        orders={"": ["missing", "participant-b", "sep:1234abcd", "participant-a"]},
        separators={"sep:1234abcd": {"name": "Backend"}},
    )

    layout.save(path)
    loaded, warning = TreeLayout.load(path)

    assert warning is None
    assert loaded.orders == layout.orders
    assert loaded.ordered(None, ["participant-a", "participant-b", "participant-c"]) == (
        "participant-b",
        "sep:1234abcd",
        "participant-a",
        "participant-c",
    )
    assert "missing" in loaded.orders[""]
    assert path.stat().st_mode & 0o777 == 0o600


def test_move_is_bounded_and_changes_only_the_requested_sibling_list() -> None:
    layout = TreeLayout(
        orders={
            "": ["root-a", "root-b", "child-a"],
            "root-a": ["ghost", "child-a", "child-b"],
        },
    )

    active = {"root-a", "root-b", "child-a", "child-b"}
    assert not layout.move(None, "root-a", -1, ["root-a", "root-b"], active)
    assert layout.move("root-a", "child-a", 1, ["child-a", "child-b"], active)
    assert not layout.move("root-a", "child-a", 1, ["child-a", "child-b"], active)
    assert layout.orders == {
        "": ["root-a", "root-b"],
        "root-a": ["child-b", "child-a"],
    }


def test_separator_mutations_keep_ids_scoped_to_their_sibling_order(monkeypatch) -> None:
    layout = TreeLayout(orders={"parent": ["child-a", "child-b"]})
    monkeypatch.setattr("regie.tree_layout.secrets.token_hex", lambda _length: "1234abcd")

    active = {"parent", "child-a", "child-b"}
    separator_id = layout.insert_separator(
        "parent", "child-b", " Backend ", ["child-a", "child-b"], active
    )

    assert separator_id == "sep:1234abcd"
    assert layout.ordered("parent", ["child-a", "child-b"]) == (
        "child-a",
        "sep:1234abcd",
        "child-b",
    )
    assert layout.rename_separator(separator_id, "Services")
    assert layout.move("parent", separator_id, -1, ["child-a", "child-b"], active)
    assert layout.ordered("parent", ["child-a", "child-b"]) == (
        "sep:1234abcd",
        "child-a",
        "child-b",
    )
    assert layout.delete_separator(separator_id)
    assert separator_id not in layout.orders["parent"]
    assert separator_id not in layout.separators


def test_corrupt_or_unknown_layout_is_ignored_without_being_overwritten(tmp_path) -> None:
    path = tmp_path / "tree-layout.json"
    original = json.dumps({"version": 99, "orders": {}, "separators": {}})
    path.write_text(original, encoding="utf-8")

    layout, warning = TreeLayout.load(path)

    assert layout == TreeLayout()
    assert warning == "tree layout ignored: unsupported or missing version"
    assert path.read_text(encoding="utf-8") == original
