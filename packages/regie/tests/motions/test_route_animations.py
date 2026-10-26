"""Await routes, send comets and the colours that tell concurrent routes apart."""

from __future__ import annotations

from regie.motions.routes import RouteAnimationController, RouteColors
from regie.render.layout import render_tree
from regie.render.routing import await_highlight_cells, send_path
from regie.ui_constants import (
    REGIE_AWAIT_ANIM_TTL,
    REGIE_AWAIT_FLASH_SECONDS,
    REGIE_ROUTE_COLORS,
    REGIE_ROUTE_TAIL_CELLS,
)

from packages.regie.tests.support.tree_nodes import PARENT

LATER = 1000.0  # far past an await's entrance flash


def _agent(pid: str, **kw) -> dict:
    return {**PARENT, "id": pid, "name": None, "status": "idle", "children": [], **kw}


def _lines(*ids: str):
    return render_tree([_agent(pid) for pid in ids])


def _tick(controller, lines, *, focus=None, now=None, resolve=None):
    return controller.tick(lines, 1, await_highlight_cells, focus=focus, now=now, resolve=resolve)


def _await(controller, lines, waiter: str, target: str, token: str = "t") -> None:
    assert controller.start_await(lines, token, f"h-{waiter}-{target}", waiter, target).started


def _glyphs(result) -> dict[tuple, tuple]:
    return {
        (key, cell): glyph
        for key, cells in result.overlays.items()
        for cell, glyph in cells.items()
    }


# ---- who is waiting on whom -------------------------------------------------


def test_each_await_marks_its_waiter_and_targets_waited_on():
    lines = _lines("aaa", "bbb", "ccc")
    controller = RouteAnimationController()
    _await(controller, lines, "aaa", "ccc")
    _await(controller, lines, "bbb", "ccc")
    _await(controller, lines, "aaa", "bbb", token="u")

    marks = controller.marks()
    assert set(marks.waiting) == {"aaa", "bbb"}
    assert marks.awaited == {"ccc", "bbb"}


def test_marks_clear_with_the_await_that_made_them():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController()
    _await(controller, lines, "aaa", "bbb")
    controller.stop_await("t", "h-aaa-bbb", "aaa", "bbb")
    assert controller.marks().waiting == {} and controller.marks().awaited == set()


def test_concurrent_waiters_get_distinct_colours_until_the_palette_runs_out():
    colors = RouteColors()
    first = [colors.color(f"agent-{n}") for n in range(len(REGIE_ROUTE_COLORS))]
    assert sorted(first) == sorted(REGIE_ROUTE_COLORS)
    assert colors.color("agent-0") == first[0]  # stable while it stays on a route

    colors.retain({"agent-1"})
    assert colors.color("newcomer") != colors.color("agent-1")  # a freed colour is reused


def test_the_marked_waiter_wears_the_colour_of_its_route():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController()
    _await(controller, lines, "aaa", "bbb")
    _tick(controller, lines, focus="aaa", now=LATER)
    (colour,) = controller.marks().waiting.values()
    assert colour in REGIE_ROUTE_COLORS


# ---- which await routes are drawn -----------------------------------------


def test_a_new_await_draws_its_route_then_leaves_only_the_marks():
    lines = _lines("aaa", "bbb", "ccc")
    controller = RouteAnimationController()
    _await(controller, lines, "aaa", "bbb")

    assert _tick(controller, lines).overlays  # still fresh: drawn for everyone
    started = next(iter(controller._await_anims.values())).started
    assert _tick(controller, lines, now=started + REGIE_AWAIT_FLASH_SECONDS + 1).overlays == {}


def test_only_the_selected_rows_awaits_stay_drawn():
    lines = _lines("aaa", "bbb", "ccc", "ddd")
    controller = RouteAnimationController()
    _await(controller, lines, "aaa", "bbb")
    _await(controller, lines, "ccc", "ddd", token="u")
    late = next(iter(controller._await_anims.values())).started + REGIE_AWAIT_FLASH_SECONDS + 1

    both = _glyphs(_tick(controller, lines, now=late))
    assert both == {}  # nobody selected, nothing fresh
    drawn = _glyphs(_tick(controller, lines, focus="bbb", now=late))
    assert drawn
    other = _glyphs(_tick(controller, lines, focus="ddd", now=late))
    assert other and not set(other) & set(drawn)  # a different row, a different route


def test_a_new_await_ends_on_the_targets_bar_in_the_waiters_colour():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController(motion=False)
    _await(controller, lines, "aaa", "bbb")

    drawn = _glyphs(_tick(controller, lines, now=0.0))

    assert drawn[(("p", "bbb"), (1, 3))] == ("▎", controller.marks().waiting["aaa"])


def test_a_stuck_await_expires_with_its_marks():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController()
    _await(controller, lines, "aaa", "bbb")
    next(iter(controller.await_anims.values())).started -= REGIE_AWAIT_ANIM_TTL + 1
    _tick(controller, lines, focus="aaa")
    assert controller.marks().waiting == {}


# ---- the await comet --------------------------------------------------------


def _styles(result) -> list[str]:
    return [
        style for _key, cells in result.overlays.items() for _cell, (_glyph, style) in cells.items()
    ]


def test_an_await_comet_runs_from_waiter_to_target_over_a_dim_route():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController()
    _await(controller, lines, "aaa", "bbb")
    heads = []
    for _ in range(8):
        styles = _styles(_tick(controller, lines, focus="aaa", now=LATER))
        assert any(style.startswith("dim ") for style in styles)  # the route at rest
        heads.append(sum(style.startswith("bold ") for style in styles))
    assert any(heads)  # the head is on the route for part of every pass


def test_reduced_motion_draws_the_route_without_a_comet():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController(motion=False)
    _await(controller, lines, "aaa", "bbb")
    for _ in range(4):
        styles = _styles(_tick(controller, lines, focus="aaa", now=LATER))
        assert styles and not any(s.startswith(("bold ", "dim ")) for s in styles)


# ---- the send comet ---------------------------------------------------------


def test_a_send_is_a_head_with_a_fading_tail_that_lands_exactly_once():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController()
    assert controller.start_route(lines, "aaa", "bbb").started
    length = len(send_path(lines, "aaa", "bbb") or [])

    landings, drawn = [], []
    while controller.has_active():
        result = _tick(controller, lines, now=LATER)
        landings += [(len(drawn), *landed) for landed in result.landed]
        drawn.append(len(_glyphs(result)))
        assert len(drawn) <= length + REGIE_ROUTE_TAIL_CELLS + 1, "a send never finished"

    ((landed_at, target, colour),) = landings
    assert target == "bbb" and colour in REGIE_ROUTE_COLORS
    assert max(drawn) == REGIE_ROUTE_TAIL_CELLS + 1
    # The head stops at the target; the tail keeps drawing until it has caught up.
    assert drawn[landed_at + 1 :] == [*range(REGIE_ROUTE_TAIL_CELLS, 0, -1), 0]


def test_the_head_is_the_brightest_cell_of_the_comet():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController()
    controller.start_route(lines, "aaa", "bbb")
    for _ in range(REGIE_ROUTE_TAIL_CELLS + 1):
        result = _tick(controller, lines, now=LATER)
    styles = sorted(_styles(result))
    assert sum(style.startswith("bold ") for style in styles) == 1


def test_reduced_motion_does_not_send_a_comet_at_all():
    lines = _lines("aaa", "bbb")
    controller = RouteAnimationController(motion=False)
    assert not controller.start_route(lines, "aaa", "bbb").started


# ---- rows folded out of sight ------------------------------------------------------------------


def _resolver(hidden: dict[str, str], visible: set[str]):
    """Ids on screen stand for themselves, folded ones for the agent hiding them."""

    def resolve(participant_id: str) -> str | None:
        if participant_id in visible:
            return participant_id
        return hidden.get(participant_id)

    return resolve


def test_an_await_on_a_folded_agent_still_marks_its_waiter_and_the_fold_that_hides_it():
    lines = _lines("aaa", "boss")
    resolve = _resolver({"kid": "boss"}, {"aaa", "boss"})
    controller = RouteAnimationController()

    assert controller.start_await(lines, "t", "h", "aaa", "kid", resolve).started
    marks = controller.marks(resolve)
    assert set(marks.waiting) == {"aaa"} and marks.awaited == {"boss"}

    # and a new await draws its route to the row standing in for the target
    assert _glyphs(_tick(controller, lines, now=0.0, resolve=resolve))


def test_a_waiter_folded_out_of_sight_leaves_no_mark_but_its_target_still_counts_it():
    lines = _lines("boss", "bbb")
    resolve = _resolver({"kid": "boss"}, {"boss", "bbb"})
    controller = RouteAnimationController()

    assert controller.start_await(lines, "t", "h", "kid", "bbb", resolve).started
    marks = controller.marks(resolve)
    assert marks.waiting == {} and marks.awaited == {"bbb"}


def test_two_agents_under_one_fold_start_a_wait_with_nothing_to_draw_until_it_opens():
    lines = _lines("boss")
    controller = RouteAnimationController()
    folded = _resolver({"kid": "boss", "kin": "boss"}, {"boss"})

    assert controller.start_await(lines, "t", "h", "kid", "kin", folded).started
    assert controller.marks(folded).waiting == {} and controller.marks(folded).awaited == set()
    assert not _glyphs(_tick(controller, lines, now=0.0, resolve=folded))  # no second row

    open_ = _resolver({}, {"boss", "kid", "kin"})  # unfolded: the wait was real all along
    assert set(controller.marks(open_).waiting) == {"kid"}
    assert controller.marks(open_).awaited == {"kin"}


def test_a_parent_awaiting_the_child_folded_under_it_still_shows_that_it_is_waiting():
    lines = _lines("boss", "other")
    folded = _resolver({"kid": "boss"}, {"boss", "other"})
    controller = RouteAnimationController()

    assert controller.start_await(lines, "t", "h", "boss", "kid", folded).started
    marks = controller.marks(folded)
    assert set(marks.waiting) == {"boss"}  # the waiting glyph on the parent
    assert marks.awaited == set()  # but no badge on itself: it is the only row there is

    unfolded = _resolver({}, {"boss", "other", "kid"})  # open the fold: the child is waited on
    marks = controller.marks(unfolded)
    assert set(marks.waiting) == {"boss"} and marks.awaited == {"kid"}


def test_an_end_that_is_nowhere_in_the_tree_is_still_refused():
    lines = _lines("aaa", "bbb")
    resolve = _resolver({}, {"aaa", "bbb"})
    assert (
        not RouteAnimationController().start_await(lines, "t", "h", "aaa", "ghost", resolve).started
    )


def test_a_fold_opening_moves_the_marks_to_the_agent_itself():
    lines = _lines("aaa", "boss", "kid")
    controller = RouteAnimationController()
    folded = _resolver({"kid": "boss"}, {"aaa", "boss"})
    assert controller.start_await(lines, "t", "h", "aaa", "kid", folded).started
    assert controller.marks(folded).awaited == {"boss"}

    unfolded = _resolver({}, {"aaa", "boss", "kid"})
    assert controller.marks(unfolded).awaited == {"kid"}


def test_a_send_to_a_folded_agent_travels_to_its_fold_and_lands_there():
    lines = _lines("aaa", "boss")
    resolve = _resolver({"kid": "boss"}, {"aaa", "boss"})
    controller = RouteAnimationController()
    assert controller.start_route(lines, "aaa", "kid", resolve).started

    landings = []
    while controller.has_active():
        landings += _tick(controller, lines, now=LATER, resolve=resolve).landed
    assert [target for target, _ in landings] == ["boss"]
