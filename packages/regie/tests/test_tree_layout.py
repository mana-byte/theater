from __future__ import annotations

import json

from regie.tree_layout import TreeLayout


def test_tree_layout_round_trips_order_and_preserves_unknown_ids(tmp_path) -> None:
    path = tmp_path / "tree-layout.json"
    layout = TreeLayout(
        orders={"": ["missing", "participant-b", "participant-a"]},
        separators={},
    )

    layout.save(path)
    loaded, warning = TreeLayout.load(path)

    assert warning is None
    assert loaded.orders == layout.orders
    assert loaded.ordered(None, ["participant-a", "participant-b", "participant-c"]) == (
        "participant-b",
        "participant-a",
        "participant-c",
    )
    assert "missing" in loaded.orders[""]
    assert path.stat().st_mode & 0o777 == 0o600


def test_move_is_bounded_and_changes_only_the_requested_sibling_list() -> None:
    layout = TreeLayout(
        orders={"": ["root-a", "root-b"], "root-a": ["child-a", "child-b"]},
    )

    assert not layout.move(None, "root-a", -1, ["root-a", "root-b"])
    assert layout.move("root-a", "child-a", 1, ["child-a", "child-b"])
    assert not layout.move("root-a", "child-a", 1, ["child-a", "child-b"])
    assert layout.orders == {
        "": ["root-a", "root-b"],
        "root-a": ["child-b", "child-a"],
    }


def test_corrupt_or_unknown_layout_is_ignored_without_being_overwritten(tmp_path) -> None:
    path = tmp_path / "tree-layout.json"
    original = json.dumps({"version": 99, "orders": {}, "separators": {}})
    path.write_text(original, encoding="utf-8")

    layout, warning = TreeLayout.load(path)

    assert layout == TreeLayout()
    assert warning == "tree layout ignored: unsupported or missing version"
    assert path.read_text(encoding="utf-8") == original
