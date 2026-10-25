"""Send-path and await-highlight routes drawn over the tree rails, and the heavy overlay."""

from __future__ import annotations

from itertools import pairwise

from regie.motions.routes import RouteAnimationController
from regie.render.glyphs import separator_label
from regie.render.layout import render_tree
from regie.render.routing import (
    DOWN,
    LEFT,
    RIGHT,
    UP,
    AwaitCell,
    await_highlight_cells,
    cell_leaf,
    heavy_rail,
    send_path,
)
from regie.ui_constants import REGIE_AWAITED_MARK as AWAITED_MARK
from regie.ui_constants import REGIE_ROUTE_COLORS

from packages.regie.tests.support.tree_nodes import (
    PARENT,
    UNMANAGED,
    label_rows,
    label_styles,
)


# ---- the send path --------------------------------------------------------
#
# The route a send animation takes has to be the one the tree draws: down a
# rail, along a branch, never across the empty space between two columns. The
# fixtures are small enough that the whole expected route can be written out,
# which is a stricter check than any property could be.
def _grid(lines) -> list[str]:
    """Every rendered row of the tree, flattened — three per leaf."""
    rows: list[str] = []
    for label, *_ in lines:
        rows += label_rows(label)
    return rows


def _char_at(grid: list[str], cell: tuple[int, int]) -> str:
    row, col = cell
    text = grid[row]
    return text[col] if col < len(text) else " "


def _contiguous(path: list[tuple[int, int]]) -> bool:
    """Whether every step is one cell up, down, left or right."""
    return all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1 for a, b in pairwise(path))


def _agent(pid: str, **kw) -> dict:
    return {**PARENT, "id": pid, "name": None, "status": "idle", "children": [], **kw}


def test_send_path_between_two_roots_runs_down_the_super_root_rail():
    """Two roots are connected only through the invisible super-root's rail.

    Written out in full: the trace leaves the sender's glyph, walks back along
    its own branch to column 0, drops down the rail the two roots share, and
    walks out along the target's branch. Any shortcut across the gap between
    columns would show up here as a missing cell.
    """
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    assert send_path(lines, "aaa", "bbb") == [
        (1, 4),  # sender's glyph
        (1, 3),
        (1, 2),
        (1, 1),
        (1, 0),  # its own ├
        (2, 0),  # the rail continuing past it
        (3, 0),  # the rail above the next root
        (4, 0),  # the target's └
        (4, 1),
        (4, 2),
        (4, 3),
        (4, 4),  # target's glyph
    ]


def test_send_route_renders_through_a_separator_before_its_target():
    separator = {"id": "sep:1234abcd", "kind": "separator", "name": "Backend"}
    lines = render_tree([_agent("aaa"), separator, _agent("bbb")])
    controller = RouteAnimationController()

    assert controller.start_route(lines, "aaa", "bbb").started
    separator_overlay = None
    while controller.has_active():
        frame = controller.tick(lines, 1, await_highlight_cells)
        if ("s", "sep:1234abcd") in frame.overlays:
            separator_overlay = frame.overlays[("s", "sep:1234abcd")]

    assert separator_overlay is not None
    rendered = separator_label("Backend", "├── ", overlay=separator_overlay)
    assert any(color in style for style in label_styles(rendered) for color in REGIE_ROUTE_COLORS)


def test_send_path_is_the_same_route_in_reverse():
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    there = send_path(lines, "aaa", "bbb")
    back = send_path(lines, "bbb", "aaa")
    assert there is not None and back is not None
    assert back == list(reversed(there))


def test_send_path_across_two_roots_only_moves_along_drawn_rails():
    """Every cell of a root-to-root route is a character the tree drew.

    No bridging is involved between two roots, so this one can be strict:
    each cell is a rail, a branch, a dash, the space that ends a branch, or
    one of the two status glyphs at the ends.
    """
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    path = send_path(lines, "aaa", "bbb")
    assert path is not None
    assert _contiguous(path)
    drawn = {_char_at(_grid(lines), cell) for cell in path[1:-1]}
    assert drawn <= set("│├└─ ")


def test_send_path_from_a_root_to_a_grandchild_descends_each_branch():
    """A route into a subtree uses one rail column per level it goes down."""
    child = _agent("xxx", children=[_agent("ccc")])
    lines = render_tree([_agent("aaa"), _agent("bbb", children=[child])])
    path = send_path(lines, "aaa", "ccc")
    assert path is not None
    assert _contiguous(path)
    assert path[0] == (1, 4)  # the sender's glyph, a root at depth 0
    assert path[-1] == (10, 12)  # the grandchild's glyph, depth 2
    # Vertical movement only ever happens on a rail column: the super-root's
    # at 0, the child's at 4, the grandchild's at 8.
    descended = {a[1] for a, b in pairwise(path) if a[0] != b[0]}
    assert descended <= {0, 4, 8}


# ---- the await highlight --------------------------------------------------
#
# An await is a claim about two agents, so the picture has to be readable as
# one: the line runs from the caller to the awaited leaf and touches nothing
# else. Two things make that hard — a route crosses junctions belonging to
# uninvolved siblings, and it is drawn over glyphs the tree already owns. The
# tests below are written against the directions each cell carries, because
# that is what decides how much of a junction may go heavy.
def _await_cells(lines, from_id: str, to_id: str) -> dict[tuple[int, int], AwaitCell]:
    """The await route keyed by cell, for assertions that ask about one place."""
    cells = await_highlight_cells(lines, from_id, to_id)
    assert cells is not None
    return {cell.cell: cell for cell in cells}


def test_await_highlight_only_tints_visible_cells_on_the_actual_route():
    """Await tinting must not spill onto ancestry rails beside the child branch."""
    lines = render_tree(
        [
            _agent("aaa", children=[_agent("side")]),
            _agent("bbb", children=[_agent("target")]),
            _agent("ccc"),
        ]
    )

    cells = _await_cells(lines, "aaa", "target")

    assert cells[(1, 0)].glyph == "├"  # the caller's own branch, leaving downward
    assert cells[(9, 4)].glyph == "│"  # the target's own incoming rail
    assert cells[(10, 4)].glyph == "└"
    assert cells[(10, 5)].glyph == "─"  # target branch reaches toward the leaf
    assert cells[(10, 6)].glyph == "─"
    assert cells[(10, 7)].glyph == AWAITED_MARK  # the branch spacer holds the target's bar
    assert (10, 8) not in cells  # status glyph
    assert (11, 0) not in cells  # below the target branch is not part of the await route
    assert (4, 4) not in cells  # passing the side leaf's ancestry rail does not light its branch
    assert (13, 0) not in cells  # the third root is not on the route at all
    assert {cell.glyph for cell in cells.values()} <= set("│├└─" + AWAITED_MARK)


def test_an_ancestor_awaiting_a_descendant_lights_its_own_run():
    """A parent awaiting its child lights the branch it departs along.

    That ``── `` run is the only horizontal connection between two depths: it
    is how the line gets from the caller's own corner to the column its
    children's rail hangs in. Suppressing it to reserve the
    ``━━``-reaching-for-a-name shape for the awaited leaf alone breaks the
    line in two, and the pulse then appears to start in mid-air below the
    caller. Continuity wins.
    """
    lines = render_tree([_agent("aaaa", children=[_agent("bbbb")])])

    cells = _await_cells(lines, "aaaa", "bbbb")

    assert cells[(1, 0)].directions == frozenset({RIGHT})  # ┕: the caller departs rightward
    assert cells[(1, 1)].directions == frozenset({LEFT, RIGHT})  # ━━ across its own branch
    assert cells[(1, 2)].directions == frozenset({LEFT, RIGHT})
    assert cells[(3, 4)].directions == frozenset({UP, DOWN})  # the rail down to the child
    assert cells[(4, 4)].directions == frozenset({UP, RIGHT})  # the awaited leaf's corner: ┗
    assert cells[(4, 6)].glyph == "─" and cells[(4, 7)].glyph == AWAITED_MARK
    assert max(col for row, col in cells if row == 4) == 7  # ends on the target's bar


def test_await_highlight_is_the_same_picture_in_reverse():
    """``a`` awaiting ``b`` and ``b`` awaiting ``a`` draw one line, not two.

    An edge is the same edge whichever side asked for it, and who waits on
    whom is told by the bus line rather than by a glyph. Pointing the heavy
    dashes at the awaited end alone left a descendant's own corner a bare
    ``┖`` where an ancestor's was a full ``┕━━`` — the same relationship
    drawn two ways.
    """
    lines = render_tree([_agent("aaaa", children=[_agent("bbbb")])])

    down = _await_cells(lines, "aaaa", "bbbb")
    up = _await_cells(lines, "bbbb", "aaaa")

    def rails(cells):
        return {c: e.directions for c, e in cells.items() if e.glyph != AWAITED_MARK}

    assert rails(down) == rails(up)  # the bar alone says which end is awaited
    assert down[(4, 7)].glyph == AWAITED_MARK and (1, 3) not in down
    assert up[(1, 3)].glyph == AWAITED_MARK and (4, 7) not in up
    assert down[(1, 0)].directions == frozenset({RIGHT})  # ┕ at the parent
    assert down[(4, 4)].directions == frozenset({UP, RIGHT})  # ┗ at the child


def test_await_highlight_passes_through_a_sibling_junction_without_claiming_it():
    """A route running vertically past a ``├`` uses two of its three arms.

    ``bbbb`` is nobody's business here: the line only needs the rail column it
    happens to share. Its rightward arm must stay light, which is what makes
    the difference between a sibling the line passes and a sibling the line
    ends on.
    """
    lines = render_tree(
        [_agent("aaaa", children=[_agent("bbbb"), _agent("cccc", children=[_agent("dddd")])])]
    )

    cells = _await_cells(lines, "aaaa", "dddd")

    assert cells[(4, 4)].glyph == "├"  # bbbb's own branch glyph
    assert cells[(4, 4)].directions == frozenset({UP, DOWN})
    assert RIGHT not in cells[(4, 4)].directions
    assert (4, 5) not in cells  # nothing of bbbb's branch is lit
    assert (4, 6) not in cells
    assert cells[(10, 8)].directions == frozenset({UP, RIGHT})  # dddd's corner, genuinely used


def test_await_highlight_across_roots_touches_only_the_roots_it_transits():
    """Crossing the super-root rail lights the way through, and nothing beside it.

    A root the route turns at is genuinely on the line — its ``── `` is how
    the route reaches the awaited subtree — so it goes heavy. What must stay
    untouched is everything the route never sets foot on: a root hanging below
    the exit, and the branch of a leaf the rail merely runs past.
    """
    lines = render_tree(
        [
            _agent("aaa", children=[_agent("side")]),
            _agent("bbb", children=[_agent("target")]),
            _agent("ccc"),
        ]
    )

    cells = _await_cells(lines, "aaa", "target")

    assert cells[(1, 0)].directions == frozenset({DOWN, RIGHT})  # ┢: departs down, reaches its name
    assert cells[(1, 1)].directions == frozenset({LEFT, RIGHT})  # …its own branch, run out to it
    assert cells[(4, 0)].directions == frozenset({UP, DOWN})  # side's row, crossed on the rail
    assert (4, 4) not in cells  # …never on its branch
    assert (13, 0) not in cells  # ccc hangs below the route's exit and is untouched
    assert cells[(7, 0)].directions == frozenset({UP, RIGHT})  # ┡: bbb is turned at, so lit
    assert cells[(10, 4)].directions == frozenset({UP, RIGHT})  # ┗ into the awaited leaf


def test_a_leaf_beside_the_route_is_never_lit_at_all():
    """The line may transit a leaf's branch; it may not touch one it never enters.

    Several leaves wear the ``━━`` shape once the route is drawn literally,
    and that is accepted — a viewer reads the line, not one glyph. What is not
    accepted is a leaf drawn into the picture without the route ever passing
    through it. ``Kid2`` hangs off a rail the route leaves before reaching it.
    """
    lines = render_tree(
        [_agent("Root", children=[_agent("Kid1", children=[_agent("Grand")]), _agent("Kid2")])]
    )

    cells = _await_cells(lines, "Root", "Grand")

    kid2 = next(index for index, line in enumerate(lines) if line[2][1] == "Kid2")
    assert not [cell for cell in cells if cell_leaf(cell)[0] == kid2]
    assert cells[(4, 4)].directions == frozenset({UP, RIGHT})  # ┡ at Kid1: down stays light
    assert (5, 4) not in cells  # …and the rail on to Kid2 is not the route's


def test_a_descendant_awaiting_an_ancestor_reaches_both_names():
    """Each end runs out along its own branch, whichever way the route arrives.

    A child awaiting its parent arrives along the parent's branch from the
    right, so the parent's dashes are on the route already and are left as
    they are. The caller leaves upward and its own ``── `` is needed for
    nothing — but stopping at the corner drew a bare ``┖`` under a leaf the
    await is entirely about, so it is extended too.
    """
    lines = render_tree([_agent("aaaa", children=[_agent("bbbb")])])

    cells = _await_cells(lines, "bbbb", "aaaa")

    assert cells[(1, 0)].directions == frozenset({RIGHT})  # ┕
    assert cells[(1, 1)].directions == frozenset({LEFT, RIGHT})  # ━
    assert cells[(1, 2)].directions == frozenset({LEFT, RIGHT})
    assert cells[(4, 4)].directions == frozenset({UP, RIGHT})  # ┗: the caller, reached too
    assert cells[(4, 5)].directions == frozenset({LEFT, RIGHT})  # ━━ out to its own name
    assert max(col for row, col in cells if row == 4) == 6  # stops at the branch's last dash


def test_await_highlight_offsets_advance_one_step_per_cell():
    """The pulse offset is the cell's place along the route, not its place in a list.

    Offsets have to grow with screen distance or the grey ramp beats against
    itself: two cells side by side would otherwise pulse half a cycle apart,
    and two cells three columns apart would pulse as neighbours.
    """
    lines = render_tree(
        [_agent("aaaa", children=[_agent("bbbb"), _agent("cccc", children=[_agent("dddd")])])]
    )

    cells = await_highlight_cells(lines, "aaaa", "dddd")
    assert cells is not None

    offsets = [cell.offset for cell in cells]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)
    # Neighbouring cells on screen are neighbouring offsets; the jumps that do
    # occur are the invisible cells the route crosses, and they are as wide as
    # the gap they stand for.
    for cell_a, cell_b in pairwise(cells):
        gap = abs(cell_a.cell[0] - cell_b.cell[0]) + abs(cell_a.cell[1] - cell_b.cell[1])
        assert cell_b.offset - cell_a.offset == gap


def test_await_highlight_never_lands_on_anything_but_a_rail():
    """Three levels deep, every lit cell is still a box-drawing character.

    The hard constraint: a status glyph, a harness name, an id or a cwd may
    never be overwritten by the highlight. Rendering the tree and reading the
    characters back is the only check that cannot be fooled by coordinates.
    """
    deep = _agent(
        "aaa", children=[_agent("bbb", children=[_agent("ccc", children=[_agent("ddd")])])]
    )
    lines = render_tree([deep, _agent("zzz")])
    grid = _grid(lines)

    for a, b in (("aaa", "ddd"), ("ddd", "aaa"), ("zzz", "ddd"), ("bbb", "ccc")):
        for cell in await_highlight_cells(lines, a, b) or ():
            drawn = _char_at(grid, cell.cell)
            if cell.glyph == AWAITED_MARK:  # the target's bar, over its blank spacer cell
                assert drawn == " "
                continue
            assert drawn in "│├└─", (a, b, cell.cell, drawn)
            assert drawn == cell.glyph


def test_the_awaited_bar_is_the_last_cell_of_the_route_and_goes_a_step_heavier():
    lines = render_tree([_agent("aaa"), _agent("bbb")])

    cells = await_highlight_cells(lines, "aaa", "bbb")
    assert cells is not None

    last = cells[-1]
    assert last.glyph == AWAITED_MARK and last.cell == (4, 3)  # bbb's spacer: past its two dashes
    assert heavy_rail(last.glyph, last.directions) == "▎"
    assert [c.glyph for c in cells if c.glyph == AWAITED_MARK] == [AWAITED_MARK]  # not aaa's end


def test_await_highlight_needs_both_ends_on_screen():
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    assert await_highlight_cells(lines, "aaa", "nope") is None
    assert await_highlight_cells(lines, None, "bbb") is None
    assert await_highlight_cells(lines, "aaa", "aaa") is None


def test_send_path_bridges_the_cwd_row_between_a_parent_and_its_child():
    """The one cell no prefix draws: a child's rail column on the parent's cwd row.

    The parent's row 3 is only as wide as the parent's own depth, so the line
    down to its children is interrupted by the cwd text. The route crosses it
    anyway — a trace that skipped the row would appear to jump.
    """
    lines = render_tree([_agent("aaa", children=[_agent("bbb")])])
    path = send_path(lines, "aaa", "bbb")
    assert path is not None
    assert _contiguous(path)
    assert (2, 4) in path  # the parent's cwd row, at the child's rail column
    assert _char_at(_grid(lines), (2, 4)) not in "│├└─"


def test_send_path_needs_both_ends_on_screen():
    """A sender or target that is not a visible row has no route to draw."""
    lines = render_tree([_agent("aaa"), _agent("bbb")])
    assert send_path(lines, "aaa", "nope") is None
    assert send_path(lines, "nope", "bbb") is None
    assert send_path(lines, None, "bbb") is None
    assert send_path(lines, "aaa", None) is None
    assert send_path([], "aaa", "bbb") is None


def test_send_path_to_oneself_is_not_a_route():
    lines = render_tree([_agent("aaa")])
    assert send_path(lines, "aaa", "aaa") is None


def test_send_path_ignores_unmanaged_panes():
    """Unmanaged rows are one row tall and have no rails; they are not endpoints."""
    lines = render_tree([_agent("aaa")], unmanaged=[UNMANAGED])
    assert send_path(lines, "aaa", UNMANAGED["pane"]) is None
