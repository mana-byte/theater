"""Tree row rendering: rails, branches, status glyphs, unmanaged panes and path shortening."""

from __future__ import annotations

from pathlib import Path

import pytest
from regie.formatting import tilde
from regie.motions.pulse import working_harness_style
from regie.render.glyphs import node_label
from regie.render.layout import render_tree, selected_participant, shorten_path
from regie.render.routing import cell_leaf
from regie.ui_constants import REGIE_SEND_TRACE_STYLE as SEND_STYLE
from regie.ui_constants import REGIE_SPINNER_FRAMES

from packages.regie.tests.support.tree_nodes import (
    CHILD,
    PARENT,
    UNMANAGED,
    label_rows,
    label_styles,
)
from tests.support.tables import eq_row, run_rows


def test_empty_tree_renders_no_participants():
    lines = render_tree([])
    assert len(lines) == 0


def test_a_human_at_the_pane_recolors_the_existing_glyph():
    """Presence changes glyph color without consuming another column."""

    def label(node: dict):
        return render_tree([node])[0][0]

    bare = label({**PARENT, "children": []})

    presence = {"state": "present", "protected": True}
    present = label({**PARENT, "human_presence": presence, "children": []})
    assert present.plain == bare.plain
    assert "$accent" in label_styles(present)
    assert "$primary" not in label_styles(present)

    unknown = {"state": "unknown", "protected": True}
    uncertain = label({**PARENT, "human_presence": unknown, "children": []})
    assert uncertain.plain == bare.plain
    assert label_styles(uncertain) == label_styles(bare)

    absent = {"state": "absent", "protected": False}
    unprotected = label({**PARENT, "human_presence": absent, "children": []})
    assert unprotected.plain == bare.plain
    assert label_styles(unprotected) == label_styles(bare)


def test_a_present_human_recolors_only_the_child_glyph():
    tree = [
        {
            **PARENT,
            "children": [{**CHILD, "human_presence": {"state": "present", "protected": True}}],
        }
    ]
    lines = render_tree(tree)
    assert "$accent" in label_styles(lines[1][0])
    assert "$accent" not in label_styles(lines[0][0])


def test_single_participant_renders_harness_and_id():
    lines = render_tree([{**PARENT, "children": []}])
    assert len(lines) == 1
    rows = label_rows(lines[0][0])
    assert len(rows) == 3
    assert rows[0] == ""  # blank leading row — first root, nothing above
    assert "vibe" in rows[1]
    assert "Arlequin" in rows[1]  # name, not short id
    assert "/tmp/proj" in rows[2]


def test_single_root_uses_last_branch():
    """A sole root is the last child of the invisible super-root: └──."""
    lines = render_tree([{**PARENT, "children": []}])
    rows = label_rows(lines[0][0])
    assert rows[1].startswith("└── ")


def test_multiple_roots_use_branch_and_last_branch():
    """Roots are siblings under the virtual super-root, connected by rails."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    first_rows = label_rows(lines[0][0])
    second_rows = label_rows(lines[1][0])
    # Non-last root: ├──
    assert first_rows[1].startswith("├── ")
    # Last root: └──
    assert second_rows[1].startswith("└── ")


def test_first_root_row1_is_blank():
    """The first root has nothing above it, so row 1 stays blank."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    assert label_rows(lines[0][0])[0] == ""


def test_later_root_row1_carries_rail_from_super_root():
    """A later root's row 1 shows the rail coming down from the virtual parent."""
    other = {**PARENT, "id": "998877665544"}
    lines = render_tree([PARENT, other])
    assert label_rows(lines[1][0])[0] == "│   "


def test_children_hang_off_a_branch():
    """Indentation alone could not tell a sibling from a nephew. Rails can."""
    lines = render_tree([{**PARENT, "children": [CHILD]}])
    assert len(lines) == 2
    parent_rows = label_rows(lines[0][0])
    child_rows = label_rows(lines[1][0])
    # Root branches off the super-root.
    assert parent_rows[1].startswith("└── ")
    # Children are one level deeper, carrying the root's continuation gap.
    assert child_rows[1].startswith("    └── ")
    assert child_rows[2].startswith("        ")


def test_only_the_last_sibling_closes_the_branch():
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    first_rows = label_rows(lines[1][0])
    second_rows = label_rows(lines[2][0])
    assert first_rows[1].startswith("    ├── ")
    # Row 3: continuation rail (non-last child shows the rail).
    assert first_rows[2].startswith("    │   ")
    assert second_rows[1].startswith("    └── ")
    # Row 3: continuation gap (last child).
    assert second_rows[2].startswith("        ")


def test_the_rail_continues_past_a_parent_that_has_siblings_below():
    """A grandchild under a non-last child still shows its aunt's line."""
    grandchild = {**CHILD, "id": "ddeeff001122"}
    first = {**CHILD, "children": [grandchild]}
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [first, second]}])
    first_rows = label_rows(lines[1][0])
    grandchild_rows = label_rows(lines[2][0])
    second_rows = label_rows(lines[3][0])
    assert first_rows[1].startswith("    ├── ")
    assert grandchild_rows[1].startswith("    │   └── ")
    # Row 3: continuation = parent's rail + gap (grandchild is last child).
    assert grandchild_rows[2].startswith("    │       ")
    assert second_rows[1].startswith("    └── ")


def test_the_rail_stops_under_a_last_child():
    grandchild = {**CHILD, "id": "ddeeff001122"}
    lines = render_tree([{**PARENT, "children": [{**CHILD, "children": [grandchild]}]}])
    child_rows = label_rows(lines[1][0])
    grandchild_rows = label_rows(lines[2][0])
    assert child_rows[1].startswith("    └── ")
    assert grandchild_rows[1].startswith("        └── ")
    # Row 3: continuation = parent's gap + gap (both last children).
    assert grandchild_rows[2].startswith("            ")


def test_row3_continuation_rail_for_middle_child_at_depth_2():
    """A non-last child at depth >= 2 shows the continuation rail on row 3.

    Row 3 must carry the continuation prefix (rail), not the branch prefix,
    so it does not look like a second node starting there.
    """
    grandchild_a = {**CHILD, "id": "ddeeff001122"}
    grandchild_b = {**CHILD, "id": "778899aabbcc"}
    parent = {**CHILD, "children": [grandchild_a, grandchild_b]}
    lines = render_tree([{**PARENT, "children": [parent]}])
    middle_rows = label_rows(lines[2][0])
    # Row 2 carries the branch.
    assert middle_rows[1].startswith("        ├── ")
    # Row 3 carries the continuation rail, not the branch.
    assert middle_rows[2].startswith("        │   ")
    assert not middle_rows[2].startswith("        ├── ")


def test_row3_continuation_gap_for_last_child_at_depth_2():
    """A last child at depth >= 2 shows the continuation gap on row 3."""
    grandchild_a = {**CHILD, "id": "ddeeff001122"}
    grandchild_b = {**CHILD, "id": "778899aabbcc"}
    parent = {**CHILD, "children": [grandchild_a, grandchild_b]}
    lines = render_tree([{**PARENT, "children": [parent]}])
    last_rows = label_rows(lines[3][0])
    # Row 2 carries the closing branch.
    assert last_rows[1].startswith("        └── ")
    # Row 3 carries the continuation gap, not the branch.
    assert last_rows[2].startswith("            ")
    assert not last_rows[2].startswith("        └── ")


def test_row3_root_has_continuation_gap():
    """A root participant branches off the super-root; row 3 carries the gap."""
    lines = render_tree([PARENT])
    rows = label_rows(lines[0][0])
    assert rows[1].startswith("└── ")
    # Row 3: continuation gap (last child of super-root).
    assert rows[2].startswith("    ")


def test_row1_carries_the_rail_into_a_childs_branch():
    """The spacing row of a child continues the line coming from the parent.

    Leaving it blank breaks the vertical rail in the gap between every pair
    of siblings, which is what the tree drawing is for.
    """
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    assert label_rows(lines[1][0])[0] == "    │   "


def test_row1_of_a_last_child_is_still_a_rail():
    """``└`` closes a line arriving from above rather than starting one.

    So a last child's spacing row is a rail like everyone else's; only its
    row 3 turns into a gap, because that is where the line actually stops.
    """
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [CHILD, second]}])
    last_rows = label_rows(lines[2][0])
    assert last_rows[0] == "    │   "
    assert last_rows[1].startswith("    └── ")
    assert last_rows[2].startswith("        ")


def test_row1_keeps_the_ancestry_of_a_grandchild():
    """A grandchild's spacing row shows its aunt's rail plus its own."""
    grandchild = {**CHILD, "id": "ddeeff001122"}
    first = {**CHILD, "children": [grandchild]}
    second = {**CHILD, "id": "778899aabbcc"}
    lines = render_tree([{**PARENT, "children": [first, second]}])
    assert label_rows(lines[2][0])[0] == "    │   │   "


def test_first_root_row1_is_blank_later_roots_carry_rail():
    """The first root's row 1 is blank; later roots carry the super-root's rail."""
    lines = render_tree([PARENT, {**PARENT, "id": "998877665544"}])
    assert label_rows(lines[0][0])[0] == ""  # first root
    assert label_rows(lines[1][0])[0] == "│   "  # later root


def test_unmanaged_panes_append_after_separator():
    lines = render_tree([PARENT], unmanaged=[UNMANAGED])
    assert len(lines) == 3  # parent + separator + unmanaged
    assert "unmanaged" in str(lines[1][0])
    assert "%9" in str(lines[2][0])


def test_unmanaged_uses_command_as_harness():
    lines = render_tree([], unmanaged=[UNMANAGED])
    assert len(lines) == 2  # separator + pane
    assert "vibe" in str(lines[1][0])


def test_unmanaged_node_falls_back_to_pane_id_when_there_is_no_name():
    """Unmanaged panes are not participants; they have no name, so the
    slot shows the short id (which is the pane id) rather than nothing."""
    lines = render_tree([], unmanaged=[UNMANAGED])
    rows = label_rows(lines[1][0])
    assert UNMANAGED["pane"] in rows[1]


def test_selected_participant_returns_the_node():
    lines = render_tree([{**PARENT, "children": [CHILD]}])
    node = selected_participant(lines, 0)
    assert node is not None
    assert node["id"] == "aabbccddeeff"
    node = selected_participant(lines, 1)
    assert node is not None
    assert node["id"] == "112233445566"


def test_selected_participant_on_separator_returns_none():
    lines = render_tree([PARENT], unmanaged=[UNMANAGED])
    # Index 1 is the separator
    assert selected_participant(lines, 1) is None


# ---- shorten_path --------------------------------------------------------
def test_shorten_path_boundaries():
    """Deep paths elide; short, root and home paths render as documented."""
    run_rows(
        [
            eq_row(
                "deeper_than_threshold_elides_prefix", lambda: shorten_path("/var/a/b/c"), "…/b/c"
            ),
            eq_row("exactly_at_threshold_unchanged", lambda: shorten_path("/b/c"), "/b/c"),
            eq_row("root_unchanged", lambda: shorten_path("/"), "/"),
            eq_row("home_relative_preserves_tilde", lambda: shorten_path("~/a/b/c"), "~/…/b/c"),
            eq_row("home_alone_is_tilde", lambda: shorten_path("~"), "~"),
            eq_row("none_returns_dash", lambda: shorten_path(None), "-"),
            eq_row("non_default_keep", lambda: shorten_path("/a/b/c/d/e", keep=3), "…/c/d/e"),
        ]
    )


# ---- status glyphs --------------------------------------------------------
def test_idle_status_uses_harness_icon():
    """Idle renders the harness's own icon, not a separate glyph column."""
    node = {**PARENT, "status": "idle", "harness": "claude"}
    lines = render_tree([node])
    rows = label_rows(lines[0][0])
    # The harness icon for claude is ✻ (or whatever harness_icon returns).
    from regie.formatting import harness_icon

    assert harness_icon("claude") in rows[1]


def test_idle_status_prefers_the_public_catalog_icon():
    node = {**PARENT, "status": "idle", "harness": "custom", "icon": "◈"}

    rows = label_rows(render_tree([node])[0][0])

    assert "◈" in rows[1]


def test_working_status_uses_braille_spinner():
    """Working renders a braille spinner frame."""
    lines = render_tree([PARENT])  # PARENT is working
    rows = label_rows(lines[0][0])

    # Row 2 is now "└── <glyph> vibe  Arlequin"; the spinner follows the branch.
    assert rows[1].split()[1] in list(REGIE_SPINNER_FRAMES)


@pytest.mark.parametrize("harness", ["claude", "codex", "opencode", "pi", "vibe"])
def test_awaiting_input_status_uses_bang(harness):
    """Awaiting input renders a bold !."""
    node = {**PARENT, "harness": harness, "status": "awaiting_input"}
    lines = render_tree([node])
    rows = label_rows(lines[0][0])
    assert "!" in rows[1]


def test_dead_status_uses_cross():
    """Dead renders ✗."""
    node = {**PARENT, "status": "dead"}
    lines = render_tree([node])
    rows = label_rows(lines[0][0])
    assert "✗" in rows[1]


def test_unknown_status_uses_question_mark():
    """An unknown status renders ?."""
    node = {**PARENT, "status": "bogus"}
    lines = render_tree([node])
    rows = label_rows(lines[0][0])
    assert "?" in rows[1]


def test_working_status_pulses_harness_letters_in_reverse():
    """Working leaves animate only the harness, one letter at a time."""

    label = node_label(PARENT, frame=0)
    pulse_styles = [style for style in label_styles(label) if style.startswith("#")]
    assert pulse_styles == [working_harness_style(0, i) for i in range(len("vibe"))]

    label = node_label(PARENT, frame=1)
    pulse_styles = [style for style in label_styles(label) if style.startswith("#")]
    assert pulse_styles == [working_harness_style(1, i) for i in range(len("vibe"))]
    assert pulse_styles[0] == working_harness_style(0, -1)


def test_non_working_statuses_do_not_pulse_harness_and_name_text():
    idle = node_label({**PARENT, "status": "idle"}, frame=0)
    awaiting = node_label({**PARENT, "status": "awaiting_input"}, frame=0)

    assert not any(style.startswith("#") for style in label_styles(idle))
    assert not any(style.startswith("#") for style in label_styles(awaiting))


def test_startup_reveal_is_applied_after_route_overlay():
    label = node_label(PARENT, overlay={(1, 20): "X"}, reveal=5)
    rows = label_rows(label)
    assert all(len(row) <= 5 for row in rows)
    assert "X" not in str(label)


# ---- cwd_segments driven by config ----------------------------------------
def test_cwd_segments_controls_path_shortening():
    """shorten_path is driven by cwd_segments, not a hardcoded 2."""
    node = {**PARENT, "cwd": "/a/b/c/d/e"}
    lines_default = render_tree([node])
    lines_keep3 = render_tree([node], cwd_segments=3)
    rows_default = label_rows(lines_default[0][0])
    rows_keep3 = label_rows(lines_keep3[0][0])
    # A single root's row 3 carries the continuation gap prefix.
    assert rows_default[2].lstrip() == "…/d/e"
    assert rows_keep3[2].lstrip() == "…/c/d/e"


def test_cell_leaf_splits_a_grid_row_into_leaf_and_row_within_it():
    assert cell_leaf((0, 4)) == (0, 0)
    assert cell_leaf((4, 4)) == (1, 1)
    assert cell_leaf((8, 0)) == (2, 2)


# ---- the overlay ----------------------------------------------------------
def test_overlay_draws_one_heavy_character_and_leaves_the_rest_alone():
    plain = label_rows(node_label(CHILD, "    └── ", cont_prefix="        "))
    label = node_label(CHILD, "    └── ", cont_prefix="        ", overlay={(1, 4): "━"})
    marked = label_rows(label)
    assert marked[1] == plain[1][:4] + "━" + plain[1][5:]
    assert len(marked[1]) == len(plain[1])
    assert marked[0] == plain[0]
    assert marked[2] == plain[2]
    assert SEND_STYLE in label_styles(label)


def test_overlay_can_draw_any_of_the_three_rows():
    label = node_label(
        CHILD,
        "    └── ",
        cont_prefix="        ",
        overlay={(0, 4): "┃", (1, 5): "━", (2, 6): "┃"},
    )
    rows = label_rows(label)
    assert rows[0][4] == "┃"
    assert rows[1][5] == "━"
    assert rows[2][6] == "┃"
    assert label_styles(label).count(SEND_STYLE) == 3


def test_overlay_past_the_end_of_a_row_is_padded():
    """A trace crossing spacer cells must not disappear for a frame."""
    rows = label_rows(node_label(CHILD, "    └── ", cont_prefix="    ", overlay={(0, 10): "┃"}))
    assert rows[0][10] == "┃"


def test_no_overlay_renders_exactly_as_before():
    assert str(node_label(PARENT, "└── ", cont_prefix="    ")) == str(
        node_label(PARENT, "└── ", cont_prefix="    ", overlay=None)
    )
    assert str(node_label(PARENT, "└── ", cont_prefix="    ", overlay={})) == str(
        node_label(PARENT, "└── ", cont_prefix="    ")
    )


def test_overlay_keeps_the_styles_of_the_text_it_splits():
    """Drawing a trace mid-run must not flatten the run's own style."""
    label = node_label(PARENT, "└── ", cont_prefix="    ", frame=0, overlay={(2, 6): "━"})
    assert "$text dim" in label_styles(label)

    assert SEND_STYLE in label_styles(label)


def test_tilde_only_abbreviates_paths_inside_home(monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: Path("/Users/bob"))

    assert tilde("/Users/bob") == "~"
    assert tilde("/Users/bob/repo") == "~/repo"
    assert tilde("/Users/bob2/repo") == "/Users/bob2/repo"
    assert tilde(None) == "-"


def _waiting_glyph(frame: int) -> str:
    return label_rows(node_label({**CHILD, "status": "working"}, frame=frame, waiting="$accent"))[
        1
    ][0]


def test_a_blocked_agent_shows_a_square_that_moves_slower_than_the_spinner():
    from regie.ui_constants import (
        REGIE_LEAF_FRAME_CYCLE,
        REGIE_WAITING_FRAMES,
        REGIE_WAITING_STEP_FRAMES,
    )

    assert set(REGIE_WAITING_FRAMES) <= set("▖▗▘▝")  # squares, like the rails around them
    assert REGIE_WAITING_STEP_FRAMES > 1  # the spinner moves every frame

    glyphs = [_waiting_glyph(frame) for frame in range(REGIE_LEAF_FRAME_CYCLE)]
    assert (
        glyphs[:REGIE_WAITING_STEP_FRAMES] == [REGIE_WAITING_FRAMES[0]] * REGIE_WAITING_STEP_FRAMES
    )
    assert len(set(glyphs)) == len(REGIE_WAITING_FRAMES)
    # The counter wraps without a stutter: the glyph after the last frame is the first's, or
    # the next in the walk, never a jump.
    steps = [
        (REGIE_WAITING_FRAMES.index(glyphs[(i + 1) % len(glyphs)]) - REGIE_WAITING_FRAMES.index(g))
        % len(REGIE_WAITING_FRAMES)
        for i, g in enumerate(glyphs)
    ]
    assert set(steps) <= {0, 1}
