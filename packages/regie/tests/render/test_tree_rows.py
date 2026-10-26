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
from regie.ui_constants import REGIE_SPINNER_FRAMES, REGIE_WORKING_HARNESS_STYLES
from rich.cells import cell_len

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


def test_a_blocked_agent_shows_a_clock_that_moves_slower_than_the_spinner():
    from regie.ui_constants import (
        REGIE_LEAF_FRAME_CYCLE,
        REGIE_WAITING_FRAMES,
        REGIE_WAITING_STEP_FRAMES,
    )

    assert all(cell_len(frame) == 1 for frame in REGIE_WAITING_FRAMES)  # never shifts the name
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


# ---- the harness is spelled out only where the row is in focus ------------------------------


def _row2(label) -> str:
    return label_rows(label)[1]


def test_without_the_harness_row_two_is_the_glyph_and_the_name():
    named = {**CHILD, "name": "builder", "harness": "claude"}
    shown = _row2(node_label(named, "├── ", show_harness=True))
    hidden = _row2(node_label(named, "├── ", show_harness=False))

    assert "claude" in shown and "claude" not in hidden
    assert hidden.endswith("builder") and hidden.startswith("├── ")
    assert shown.endswith("claude  builder")


@pytest.mark.parametrize(
    ("name", "focused", "shown"),
    [
        ("builder", False, False),
        ("builder", True, True),
        (None, False, True),  # no name of its own: the id says little, the harness a lot
        ("", False, True),
    ],
)
def test_the_harness_shows_on_the_focused_row_and_wherever_there_is_no_name(name, focused, shown):
    from regie.render.glyphs import harness_shown

    assert harness_shown({**CHILD, "name": name}, focused=focused) is shown


def _name_spans(label, name: str):
    """The spans styling *name*'s characters on row two."""
    rows = label_rows(label)
    begin = len(rows[0]) + 1 + rows[1].index(name)
    return [span for span in label.spans if begin <= span.start < begin + len(name)]


def test_a_working_agent_pulses_its_name_not_its_hidden_harness():
    working = {**CHILD, "name": "builder", "status": "working", "harness": "claude"}
    label = node_label(working, "├── ", frame=3, show_harness=False)
    assert "claude" not in _row2(label) and _row2(label).endswith("builder")

    styles = [str(span.style) for span in _name_spans(label, "builder")]
    assert len(styles) == len("builder") and set(styles) <= set(REGIE_WORKING_HARNESS_STYLES)
    assert len(set(styles)) > 1  # a wave along the name, not one flat colour


def test_with_the_harness_shown_the_pulse_stays_on_the_harness_as_before():
    working = {**CHILD, "name": "builder", "status": "working", "harness": "claude"}
    label = node_label(working, "├── ", frame=3, show_harness=True)
    rows = label_rows(label)
    harness_begin = len(rows[0]) + 1 + rows[1].index("claude")
    pulsed = [
        span for span in label.spans if harness_begin <= span.start < harness_begin + len("claude")
    ]
    assert {str(span.style) for span in pulsed} <= set(REGIE_WORKING_HARNESS_STYLES) and pulsed
    assert not [
        span
        for span in _name_spans(label, "builder")
        if str(span.style) in REGIE_WORKING_HARNESS_STYLES
    ]


def test_an_unreachable_names_italic_survives_the_pulse():
    unreachable = {**CHILD, "name": "ghost", "status": "working", "addressable": False}
    label = node_label(unreachable, "├── ", frame=0, show_harness=False)
    styles = [str(span.style) for span in _name_spans(label, "ghost")]
    assert len(styles) == len("ghost") and all(style.startswith("italic ") for style in styles)
    assert all(style.split()[-1] in REGIE_WORKING_HARNESS_STYLES for style in styles)


def test_a_blocked_agent_does_not_pulse_its_name():
    blocked = {**CHILD, "name": "builder", "status": "working"}
    label = node_label(blocked, "├── ", frame=3, waiting="$accent", show_harness=False)
    assert not [
        span
        for span in _name_spans(label, "builder")
        if str(span.style) in REGIE_WORKING_HARNESS_STYLES
    ]


def test_the_name_starts_earlier_when_the_harness_is_hidden():
    from regie.render.glyphs import visible_name_span

    named = {**CHILD, "name": "builder", "harness": "claude"}
    shown = visible_name_span(named, "├── ", show_harness=True)
    hidden = visible_name_span(named, "├── ", show_harness=False)
    assert shown is not None and hidden is not None
    assert shown[0] - hidden[0] == len("claude") + 1  # the harness and its gap, less one space


# ---- a name too long for row two ends in an ellipsis instead of wrapping -----------------------

LONG = "a-really-quite-long-participant-name-that-keeps-going"


def test_a_name_that_fits_is_left_alone_and_one_that_does_not_ends_in_an_ellipsis():
    from regie.render.glyphs import fit_name

    assert fit_name("builder", 7) == "builder" and fit_name("builder", 20) == "builder"
    assert fit_name("builder", 5) == "buil…" and fit_name("builder", 1) == "…"
    assert cell_len(fit_name("構築構築構築", 5)) <= 5  # wide characters count by cells


def test_the_room_for_a_name_shrinks_with_the_harness_the_fold_badge_and_the_mark():
    from regie.marks import MarkKind, RowMark
    from regie.render.glyphs import name_room

    named = {**CHILD, "name": "builder", "harness": "claude"}
    plain = name_room(named, "├── ", width=40, show_harness=False)
    with_harness = name_room(named, "├── ", width=40, show_harness=True)
    with_mark = name_room(
        {**named, "mark": RowMark(MarkKind.SENT, "op", "✓", "$success")},
        "├── ",
        width=40,
        show_harness=False,
    )
    folded = name_room(
        {**named, "hidden_count": 3, "hidden_states": {"working": 1}},
        "├── ",
        width=40,
        show_harness=False,
    )
    assert plain is not None and with_harness is not None
    assert plain - with_harness == len("claude") + 1
    assert with_mark is not None and plain - with_mark == len("  ✓")
    assert folded is not None and plain - folded == len("  ▸3 ⠋1")
    assert name_room(named, "├── ", width=None, show_harness=False) is None


@pytest.mark.parametrize("width", [33, 48])
def test_row_two_never_outgrows_the_width_and_keeps_its_badges(width):
    from regie.marks import MarkKind, RowMark

    node = {
        **CHILD,
        "name": LONG,
        "mark": RowMark(MarkKind.BLOCKED, "op", "⊘", "$warning", "blocked · busy"),
    }
    row = _row2(node_label(node, "├── ", width=width, awaited=True, show_harness=True))
    assert cell_len(row) <= width
    assert "…" in row and row.endswith("  ⊘")  # the name gave way, the mark did not
    assert row.startswith("├──▏")  # the wait is in the connector: it takes no room from the name


def test_without_a_width_the_whole_name_is_drawn_and_a_callers_window_wins():
    node = {**CHILD, "name": LONG}
    assert _row2(node_label(node, "├── ")).endswith(LONG)
    assert _row2(node_label(node, "├── ", width=30, name_text="window")).endswith("window")


def test_a_row_too_narrow_even_for_its_badges_is_cropped_not_wrapped():
    node = {**CHILD, "name": LONG}
    assert cell_len(_row2(node_label(node, "│   │   ├── ", width=18, show_harness=True))) <= 18


# ---- the fold badge -------------------------------------------------------------------------


def _folded(count: int, **states: int) -> dict:
    ordered = {k: states[k] for k in ("dead", "awaiting_input", "working") if k in states}
    return {**CHILD, "name": "boss", "hidden_count": count, "hidden_states": ordered}


def test_a_folded_row_says_how_many_it_hides_and_what_is_going_on_there_worst_first():
    spinner = REGIE_SPINNER_FRAMES[0]
    cases = [
        (_folded(3), "boss  ▸3"),
        (_folded(3, working=1), f"boss  ▸3 {spinner}1"),
        (_folded(5, awaiting_input=1, working=2), f"boss  ▸5 !1 {spinner}2"),
        (_folded(4, dead=1), "boss  ▸4 ✗1"),
        (_folded(9, working=3, dead=2, awaiting_input=1), f"boss  ▸9 ✗2 !1 {spinner}3"),
    ]
    for node, tail in cases:
        assert _row2(node_label(node, "├── ", show_harness=False, frame=0)).endswith(tail)


def test_each_state_in_the_fold_badge_wears_its_own_colour():
    label = node_label(_folded(6, dead=1, awaiting_input=2, working=3), "├── ", show_harness=False)
    styles = label_styles(label)
    assert {"$error", "$warning", "$primary", "$text-muted"} <= set(styles)
    calm = label_styles(node_label(_folded(2), "├── ", show_harness=False))
    assert "$text-muted" in calm and not {"$error", "$warning", "$primary"} & set(calm)


def test_the_fold_badge_shortens_before_the_name_loses_its_room():
    # The row is a 4-cell connector, the glyph and its 2-cell gap (7), the name, then the badge.
    # The name keeps 10 cells (its own length, when shorter); the badge gives way before that.
    node = {**_folded(12, dead=1, awaiting_input=2, working=9), "name": "orchestrator"}
    spinner = REGIE_SPINNER_FRAMES[0]

    def tail(width: int) -> str:
        row = _row2(node_label(node, "├── ", width=width, show_harness=False, frame=0))
        assert cell_len(row) <= width
        return row[row.index("  ▸") :]

    assert tail(60) == f"  ▸12 ✗1 !2 {spinner}9"  # all the room it wants
    assert tail(31) == f"  ▸12 ✗1 !2 {spinner}9"  # exactly leaves the name its 10
    assert tail(30) == "  ▸12 ✗"  # the counts go first; the worst glyph stays
    assert tail(24) == "  ▸12 ✗"
    assert tail(23) == "  ▸12"  # then the glyph; the arrow and the total never go
    assert tail(18) == "  ▸12"  # even once the name has to be cut for it


def test_an_open_row_shows_its_chevron_only_when_asked_and_only_with_children():
    parent = {**CHILD, "name": "boss", "children": [{**CHILD, "id": "kid"}]}
    assert _row2(node_label(parent, "├── ", show_harness=False, fold_hint=True)).endswith("boss  ▾")
    assert _row2(node_label(parent, "├── ", show_harness=False)).endswith("boss")
    childless = {**CHILD, "name": "solo"}
    assert _row2(node_label(childless, "├── ", show_harness=False, fold_hint=True)).endswith("solo")


def test_a_long_name_leaves_room_for_the_fold_badge():
    folded = {**CHILD, "name": LONG, "hidden_count": 12, "hidden_states": {"working": 4}}
    row = _row2(node_label(folded, "├── ", width=36, show_harness=False))
    assert cell_len(row) <= 36 and "…" in row and "▸12" in row


def test_a_short_name_keeps_the_full_badge_where_a_long_one_would_force_it_down():
    spinner = REGIE_SPINNER_FRAMES[0]
    states = {"dead": 1, "awaiting_input": 2, "working": 9}
    short = {**CHILD, "name": "ab", "hidden_count": 12, "hidden_states": states}
    long = {**short, "name": LONG}
    assert _row2(node_label(short, "├── ", width=30, show_harness=False)).endswith(
        f"▸12 ✗1 !2 {spinner}9"
    )
    assert not _row2(node_label(long, "├── ", width=30, show_harness=False)).endswith(
        f"▸12 ✗1 !2 {spinner}9"
    )


def test_an_agent_peers_wait_on_wears_a_double_arrow_in_its_connector():
    row = lambda awaited: _row2(  # noqa: E731
        node_label({**CHILD, "name": "tester"}, "├── ", awaited=awaited)
    )
    assert row(False).startswith("├── ") and "▏" not in row(False)
    assert row(True).startswith("├──▏")
    assert len(row(True)) == len(row(False))  # the connector keeps its width


def test_the_awaited_marker_leaves_a_rooted_row_without_a_connector_alone():
    assert "▏" not in _row2(node_label({**CHILD, "name": "x"}, "", awaited=True))


# ---- the cost lives on row three -----------------------------------------------------------


COST = [("$", "$text dim"), ("12.34", "$text dim")]


def _row3(label) -> str:
    return label_rows(label)[2]


def test_the_cost_is_right_aligned_on_row_three_and_row_two_stays_free_of_it():
    node = {**CHILD, "name": "orchestrator", "cwd": "/p/api"}
    label = node_label(node, "├── ", cont_prefix="│   ", width=40, cost=COST, show_harness=False)
    assert "$12.34" not in _row2(label)
    assert _row3(label).endswith("$12.34") and cell_len(_row3(label)) == 40
    assert _row3(label).startswith("│   ") and "/p/api" in _row3(label)


def test_a_row_two_full_of_badges_still_shows_its_cost():
    # What used to drop the cost: a long name, a fold badge and a mark, all on row two.
    from regie.marks import MarkKind, RowMark

    node = {
        **CHILD,
        "name": LONG,
        "cwd": "/p/api",
        "hidden_count": 5,
        "hidden_states": {"awaiting_input": 1, "working": 2},
        "mark": RowMark(MarkKind.SENT, "op", "✓", "$success"),
    }
    label = node_label(node, "│   ├── ", cont_prefix="│   │   ", width=38, cost=COST)
    assert "$12.34" in _row3(label) and "$12.34" not in _row2(label)


def test_a_long_directory_gives_way_to_the_cost_not_the_other_way_round():
    from regie.render.glyphs import fit_name

    detail = fit_name("/Users/me/some/deep/project/service", 20)  # what the leaf passes in
    label = node_label(
        {**CHILD, "name": "x"}, "├── ", cont_prefix="│   ", width=40, cost=COST, detail=detail
    )
    assert _row3(label).endswith("$12.34") and cell_len(_row3(label)) <= 40


def test_a_marks_reason_is_cut_to_leave_room_for_the_cost_and_never_wraps():
    from regie.marks import MarkKind, RowMark

    reason = "blocked · awaiting a decision from the human at the keyboard"
    node = {
        **CHILD,
        "name": "x",
        "mark": RowMark(MarkKind.BLOCKED, "op", "⊘", "$warning", reason),
    }
    label = node_label(node, "├── ", cont_prefix="│   ", width=36, cost=COST)
    row3 = _row3(label)
    assert "…" in row3 and row3.endswith("$12.34") and cell_len(row3) <= 36


def test_the_hint_stays_on_row_two_for_the_row_that_asks_for_a_directory():
    label = node_label(
        {**CHILD, "name": "new agent"}, "└── ", width=40, hint=[("⏎ spawn", "$text-muted")]
    )
    assert _row2(label).endswith("⏎ spawn") and "⏎" not in _row3(label)
