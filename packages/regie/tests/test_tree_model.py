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


# ---- folding an agent's children -------------------------------------------------------------


def _family(statuses: dict[str, str] | None = None) -> StateProjection:
    """root -> (a -> a1, b), plus an unrelated other."""
    parents = {"a": "root", "a1": "a", "b": "root"}
    people = {"root": _participant("root", name="root"), "other": _participant("other")}
    for pid, parent in parents.items():
        people[pid] = _participant(pid, parent_id=parent, name=pid)
    for pid, status in (statuses or {}).items():
        people[pid] = Participant.from_wire({**people[pid].to_wire(), "status": status})
    return StateProjection(
        cursor=EventCursor("stream", 1),
        participants=MappingProxyType(people),
        operations=MappingProxyType({}),
        jobs=MappingProxyType({}),
        providers=MappingProxyType({}),
        workspaces=MappingProxyType({}),
    )


def _flatten(nodes) -> list[str]:
    return [item for node in nodes for item in (node["id"], *_flatten(node["children"]))]


def test_a_folded_participant_hides_every_descendant_and_counts_them() -> None:
    layout = TreeLayout(folded={"root"})
    forest = tree_for_projection(_family(), layout=layout)

    assert _flatten(forest) == ["root", "other"]
    root = forest[0]
    assert root["hidden_count"] == 3  # a, a1 and b: every level, not just the children
    assert sorted(root["hidden_ids"]) == ["a", "a1", "b"]
    assert root["hidden_status"] is None and root["children"] == []


def test_folding_an_inner_agent_leaves_the_rest_of_the_tree_alone() -> None:
    forest = tree_for_projection(_family(), layout=TreeLayout(folded={"a"}))
    assert _flatten(forest) == ["root", "a", "b", "other"]
    assert forest[0]["children"][0]["hidden_count"] == 1


def test_a_fold_shows_the_worst_state_it_hides() -> None:
    awaiting = tree_for_projection(
        _family({"a1": "awaiting_input", "b": "working"}), layout=TreeLayout(folded={"root"})
    )
    assert awaiting[0]["hidden_status"] == "awaiting_input"
    working = tree_for_projection(_family({"b": "working"}), layout=TreeLayout(folded={"root"}))
    assert working[0]["hidden_status"] == "working"


def test_a_participant_with_nothing_below_it_has_nothing_to_fold() -> None:
    forest = tree_for_projection(_family(), layout=TreeLayout(folded={"other", "b"}))
    assert "hidden_count" not in forest[1] and _flatten(forest) == _flatten(
        tree_for_projection(_family())
    )


def test_a_fold_counts_what_a_folded_section_below_it_already_hides() -> None:
    layout = TreeLayout(
        orders={"root": ["sep:1234abcd", "a", "b"]},
        separators={"sep:1234abcd": {"name": "Later", "collapsed": True}},
        folded={"root"},
    )
    forest = tree_for_projection(_family(), layout=layout)
    assert forest[0]["hidden_count"] == 3  # the section's agents are still below root


def test_folds_are_saved_only_while_there_are_any_and_forgotten_when_the_agent_is_gone(
    tmp_path,
) -> None:
    path = tmp_path / "tree-layout.json"
    layout = TreeLayout()
    layout.save(path)
    assert "folded" not in json.loads(path.read_text())  # files stay as older Régies wrote them

    assert layout.toggle_fold("root") is True and layout.toggle_fold("a") is True
    layout.save(path)
    loaded, warning = TreeLayout.load(path)
    assert warning is None and loaded.folded == {"root", "a"}

    loaded.prune_folded(["root", "other"])
    assert loaded.folded == {"root"}
    assert loaded.toggle_fold("root") is False and loaded.folded == set()


def test_a_corrupt_folded_list_discards_the_layout_without_overwriting_it(tmp_path) -> None:
    path = tmp_path / "tree-layout.json"
    original = json.dumps({"version": 1, "orders": {}, "separators": {}, "folded": [1]})
    path.write_text(original, encoding="utf-8")

    layout, warning = TreeLayout.load(path)

    assert layout == TreeLayout() and warning is not None and "folded" in warning
    assert path.read_text(encoding="utf-8") == original


def test_an_outer_fold_counts_and_summarises_what_an_inner_fold_already_hides() -> None:
    forest = tree_for_projection(
        _family({"a1": "awaiting_input"}), layout=TreeLayout(folded={"a", "root"})
    )
    root = forest[0]
    assert root["hidden_count"] == 3 and sorted(root["hidden_ids"]) == ["a", "a1", "b"]
    assert root["hidden_status"] == "awaiting_input"  # the inner fold's trouble is not lost


def test_a_fold_counts_hidden_agents_by_state_worst_first_each_agent_once() -> None:
    from regie.marks import MarkKind, RowMark

    statuses = {"a": "working", "a1": "awaiting_input", "b": "dead"}
    forest = tree_for_projection(_family(statuses), layout=TreeLayout(folded={"root"}))
    assert forest[0]["hidden_states"] == {"dead": 1, "awaiting_input": 1, "working": 1}

    # A blocked send beside a working turn counts once, as the thing that needs the human.
    marks = {"a": RowMark(MarkKind.BLOCKED, "op", "⊘", "$warning", "blocked · busy")}
    marked = tree_for_projection(
        _family({"a": "working"}), layout=TreeLayout(folded={"root"}), marks=marks
    )
    assert marked[0]["hidden_states"] == {"awaiting_input": 1}

    calm = tree_for_projection(_family(), layout=TreeLayout(folded={"root"}))
    assert calm[0]["hidden_states"] == {}
