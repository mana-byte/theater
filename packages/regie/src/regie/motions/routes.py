"""Tree-route animation state, glyph mechanics, and the route animation controller.

The controller never sees ``RegieApp`` or Textual; the app alone owns timers and
``TreePanel.set_overlays``.
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field

from regie.render.glyphs import (
    LeafCell,
    OverlayGlyph,
)
from regie.render.layout import Key
from regie.render.routing import (
    AwaitCell,
    Cell,
    Direction,
    await_path,
    cell_leaf,
    heavy_rail,
    send_path,
)
from regie.ui_constants import (
    REGIE_AWAIT_ANIM_TTL,
    REGIE_AWAIT_COMET_GAP_FRAMES,
    REGIE_AWAIT_FLASH_SECONDS,
    REGIE_MAX_AWAIT_ANIMS,
    REGIE_MAX_TRACE_ANIMS,
    REGIE_ROUTE_COLORS,
    REGIE_ROUTE_TAIL_CELLS,
)

#: The visible row standing for a participant (itself, or the fold hiding it); None if none does.
type Resolve = Callable[[str], str | None]

#: Heavy trace glyphs to draw within one leaf: ``(row within the leaf, column)``.
type LeafOverlay = dict[LeafCell, OverlayGlyph]

#: Heavy line glyphs by which route directions pass through a cell.
_SEND_TRACE_GLYPHS = {
    frozenset({(0, -1)}): "━",
    frozenset({(0, 1)}): "━",
    frozenset({(-1, 0)}): "┃",
    frozenset({(1, 0)}): "┃",
    frozenset({(0, -1), (0, 1)}): "━",
    frozenset({(-1, 0), (1, 0)}): "┃",
    frozenset({(-1, 0), (0, 1)}): "┗",
    frozenset({(-1, 0), (0, -1)}): "┛",
    frozenset({(1, 0), (0, 1)}): "┏",
    frozenset({(1, 0), (0, -1)}): "┓",
}


class RouteAnim:
    """One trace travelling from a sender's leaf to its target's.

    Stores ids and a step, never the route: the tree refreshes every second and a
    stored route goes stale when rows shift. Recomputing per frame keeps it sensible.
    """

    def __init__(self, from_id: str, to_id: str) -> None:
        self.from_id = from_id
        self.to_id = to_id
        self.step = 0


class AwaitRouteAnim:
    """One active await relationship pulsing along a visible tree route.

    Expires after :data:`REGIE_AWAIT_ANIM_TTL` because a missing ``job.await.end``
    row would otherwise leave it pulsing for the rest of the session.
    """

    def __init__(
        self, token: str, handle: str, from_id: str, to_id: str, started: float | None = None
    ) -> None:
        self.token = token
        self.handle = handle
        self.from_id = from_id
        self.to_id = to_id
        self.frame = 0
        self.started = time.monotonic() if started is None else started

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.token, self.handle, self.from_id, self.to_id)

    def expired(self, now: float) -> bool:
        return now - self.started >= REGIE_AWAIT_ANIM_TTL

    def fresh(self, now: float) -> bool:
        return now - self.started < REGIE_AWAIT_FLASH_SECONDS


def _send_trace_glyph(path: list[Cell], index: int) -> str:
    """A heavy line glyph matching how the route passes through *index*."""
    row, col = path[index]
    directions: set[Direction] = set()
    for neighbor_index in (index - 1, index + 1):
        if 0 <= neighbor_index < len(path):
            next_row, next_col = path[neighbor_index]
            directions.add((next_row - row, next_col - col))
    return _SEND_TRACE_GLYPHS.get(frozenset(directions), "━")


def _comet_style(color: str, behind: int | None) -> str:
    """The style of a route cell *behind* cells behind the head, negative when the head has yet to
    reach it; None draws the route at rest."""
    if behind is None:
        return color
    if behind < 0:
        return f"dim {color}"
    if behind == 0:
        return f"bold {color}"
    return color if behind <= REGIE_ROUTE_TAIL_CELLS else f"dim {color}"


class RouteColors:
    """Give each participant on a route one of a few colours, and keep it while it stays on one.

    The least-used colour goes first, so up to four routes at once never share a colour.
    """

    def __init__(self) -> None:
        self._assigned: dict[str, str] = {}

    def color(self, participant_id: str) -> str:
        if participant_id not in self._assigned:
            used = Counter(self._assigned.values())
            self._assigned[participant_id] = min(
                REGIE_ROUTE_COLORS, key=lambda color: (used[color], REGIE_ROUTE_COLORS.index(color))
            )
        return self._assigned[participant_id]

    def retain(self, participant_ids: Collection[str]) -> None:
        self._assigned = {
            pid: color for pid, color in self._assigned.items() if pid in participant_ids
        }


@dataclass(frozen=True, slots=True)
class AwaitMarks:
    """Who is blocked on a peer, in the colour of that await, and who is being waited on."""

    waiting: Mapping[str, str] = field(default_factory=dict)
    awaited: Mapping[str, int] = field(default_factory=dict)
    #: The peers each blocked agent waits on, and the agents waiting on each row, by id.
    waiting_on: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    waited_by: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass
class StartRouteDecision:
    """Whether a send trace started, and why it might not have."""

    started: bool = False


@dataclass
class StartAwaitDecision:
    """Whether an await pulse started, and why it might not have."""

    started: bool = False


@dataclass
class StopAwaitDecision:
    """Whether an await pulse stopped, and whether overlays should clear."""

    stopped: bool = False
    clear_overlays: bool = False
    stop_timer: bool = False


@dataclass
class TickResult:
    """Overlays to draw and whether the animation timer should stop."""

    overlays: dict[Key, LeafOverlay] = field(default_factory=dict)
    stop_timer: bool = False
    #: ``(target id, sender colour)`` of every send whose head reached its target this frame.
    landed: list[tuple[str, str]] = field(default_factory=list)


class RouteAnimationController:
    """Owns route/await collections, TTL reaping, and revision cache.

    Returns decisions only; the app performs every Textual side effect.
    """

    def __init__(self, *, motion: bool = True) -> None:
        self._motion = motion
        self._route_anims: list[RouteAnim] = []
        self._await_anims: dict[tuple[str, str, str, str], AwaitRouteAnim] = {}
        self._await_cells: dict[tuple[str, str], list[AwaitCell] | None] = {}
        self._await_cells_revision: int = -1
        self._colors = RouteColors()

    @property
    def route_anims(self) -> list[RouteAnim]:
        return self._route_anims

    @property
    def await_anims(self) -> dict[tuple[str, str, str, str], AwaitRouteAnim]:
        return self._await_anims

    def has_active(self) -> bool:
        """Whether any animation is in flight."""
        return bool(self._route_anims or self._await_anims)

    @staticmethod
    def _rows(
        from_id: str | None, to_id: str | None, resolve: Resolve | None
    ) -> tuple[str, str] | None:
        """The rows standing for both ends of a relation, which may be one row (a parent and the
        child folded under it); None when either end is nowhere in the tree."""
        if resolve is not None and from_id and to_id:
            from_id, to_id = resolve(from_id), resolve(to_id)
        if not from_id or not to_id:
            return None
        return from_id, to_id

    @classmethod
    def _ends(
        cls, from_id: str | None, to_id: str | None, resolve: Resolve | None
    ) -> tuple[str, str] | None:
        """The two rows a route runs between; None when there are not two (an end out of the
        tree, or both under one fold): nothing to draw, though the wait still counts."""
        rows = cls._rows(from_id, to_id, resolve)
        return None if rows is None or rows[0] == rows[1] else rows

    def marks(self, resolve: Resolve | None = None) -> AwaitMarks:
        """The waiting and waited-on rows right now, for the end marks every await leaves.

        A waiter folded out of sight leaves no mark; a target folded out of sight is counted on
        the row that stands for it.
        """
        waiting: dict[str, str] = {}
        targets: dict[str, set[str]] = {}
        waiters: dict[str, set[str]] = {}
        for anim in self._await_anims.values():
            rows = self._rows(anim.from_id, anim.to_id, resolve)
            if rows is None:
                continue
            from_row, to_row = rows
            if from_row == anim.from_id:  # a waiter folded out of sight leaves no mark
                waiting.setdefault(anim.from_id, self._colors.color(anim.from_id))
                targets.setdefault(anim.from_id, set()).add(anim.to_id)
            if to_row != from_row:  # one row has nothing to be waited on by itself
                waiters.setdefault(to_row, set()).add(anim.from_id)
        return AwaitMarks(
            waiting,
            {target: len(who) for target, who in waiters.items()},
            {waiter: tuple(sorted(peers)) for waiter, peers in targets.items()},
            {target: tuple(sorted(who)) for target, who in waiters.items()},
        )

    def _release_colors(self) -> None:
        active = {anim.from_id for anim in self._await_anims.values()}
        active.update(anim.from_id for anim in self._route_anims)
        self._colors.retain(active)

    def start_route(
        self,
        tree_lines: list[tuple],
        from_id: str | None,
        to_id: str | None,
        resolve: Resolve | None = None,
    ) -> StartRouteDecision:
        """Begin a trace, if a route exists and the cap has not been reached."""
        if not self._motion or len(self._route_anims) >= REGIE_MAX_TRACE_ANIMS:
            return StartRouteDecision(started=False)
        ends = self._ends(from_id, to_id, resolve)
        if ends is None or send_path(tree_lines, *ends) is None:
            return StartRouteDecision(started=False)
        assert from_id and to_id
        self._route_anims.append(RouteAnim(from_id, to_id))
        return StartRouteDecision(started=True)

    def start_await(
        self,
        tree_lines: list[tuple],
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
        resolve: Resolve | None = None,
    ) -> StartAwaitDecision:
        """Begin an await pulse, reaping expired ones first.

        With a *resolve*, an end folded out of sight is judged by the row standing for it: the
        wait still marks its waiter even when there is no route to draw: a parent awaiting the
        child folded under it has no second row, and its glyph must still show the wait.
        """
        self._reap_await_anims()
        if len(self._await_anims) >= REGIE_MAX_AWAIT_ANIMS:
            return StartAwaitDecision(started=False)
        if not token or not handle or not from_id or not to_id or from_id == to_id:
            return StartAwaitDecision(started=False)
        rows = self._rows(from_id, to_id, resolve)
        if rows is None or (resolve is None and await_path(tree_lines, *rows) is None):
            return StartAwaitDecision(started=False)
        anim = AwaitRouteAnim(str(token), str(handle), from_id, to_id)
        self._await_anims[anim.key] = anim
        return StartAwaitDecision(started=True)

    def stop_await(
        self,
        token: object,
        handle: object,
        from_id: str | None,
        to_id: str | None,
    ) -> StopAwaitDecision:
        """End one await pulse; clear overlays if nothing remains."""
        if not token or not handle or not from_id or not to_id:
            return StopAwaitDecision()
        self._await_anims.pop((str(token), str(handle), from_id, to_id), None)
        self._release_colors()
        if self._route_anims or self._await_anims:
            return StopAwaitDecision(stopped=True)
        return StopAwaitDecision(stopped=True, clear_overlays=True, stop_timer=True)

    def _reap_await_anims(self) -> None:
        """Drop pulses whose end row never arrived."""
        now = time.monotonic()
        for key, anim in list(self._await_anims.items()):
            if anim.expired(now):
                del self._await_anims[key]
        self._release_colors()

    def _await_route_cells(
        self,
        tree_lines: list[tuple],
        tree_revision: int,
        from_id: str,
        to_id: str,
        highlight_fn,
    ) -> list[AwaitCell] | None:
        """The await route's visible cells, computed once per tree revision.

        ``highlight_fn`` is resolved/passed at call time to preserve the
        monkeypatch seam in ``regie.app``.
        """
        if self._await_cells_revision != tree_revision:
            self._await_cells.clear()
            self._await_cells_revision = tree_revision
        key = (from_id, to_id)
        if key not in self._await_cells:
            self._await_cells[key] = highlight_fn(tree_lines, from_id, to_id)
        return self._await_cells[key]

    def tick(
        self,
        tree_lines: list[tuple],
        tree_revision: int,
        highlight_fn,
        *,
        focus: str | None = None,
        now: float | None = None,
        resolve: Resolve | None = None,
    ) -> TickResult:
        """Compute overlays for the current frame and advance all anims.

        Only a new await and the *focus* row's awaits draw their route; every other await is
        left to the end marks, so a crowd of waits stays readable.

        ``highlight_fn`` is passed at call time to preserve the monkeypatch seam in ``regie.app``.
        """
        current = time.monotonic() if now is None else now
        self._reap_await_anims()
        overlays: dict[Key, LeafOverlay] = {}
        for await_anim in self._await_anims.values():
            ends = self._ends(await_anim.from_id, await_anim.to_id, resolve)
            if ends is not None and (focus in ends or await_anim.fresh(current)):
                self._draw_await(
                    overlays, tree_lines, tree_revision, await_anim, ends, highlight_fn
                )
            await_anim.frame += 1
        landed = self._draw_sends(overlays, tree_lines, resolve)
        stop_timer = not self._route_anims and not self._await_anims
        return TickResult(overlays=overlays, stop_timer=stop_timer, landed=landed)

    def _draw_await(
        self,
        overlays: dict[Key, LeafOverlay],
        tree_lines: list[tuple],
        tree_revision: int,
        anim: AwaitRouteAnim,
        ends: tuple[str, str],
        highlight_fn,
    ) -> None:
        """One await: the route in its waiter's colour, a comet running waiter to target."""
        cells = self._await_route_cells(tree_lines, tree_revision, *ends, highlight_fn)
        if not cells:
            return
        color = self._colors.color(anim.from_id)
        length = cells[-1].offset + 1
        head = anim.frame % (length + REGIE_ROUTE_TAIL_CELLS + REGIE_AWAIT_COMET_GAP_FRAMES)
        for await_cell in cells:
            heavy = heavy_rail(await_cell.glyph, await_cell.directions)
            leaf_index, row_in_leaf = cell_leaf(await_cell.cell, tree_lines)
            if heavy == await_cell.glyph or not 0 <= leaf_index < len(tree_lines):
                continue
            style = _comet_style(color, head - await_cell.offset if self._motion else None)
            key = tree_lines[leaf_index][2]
            overlays.setdefault(key, {})[(row_in_leaf, await_cell.cell[1])] = (heavy, style)

    def _draw_sends(
        self,
        overlays: dict[Key, LeafOverlay],
        tree_lines: list[tuple],
        resolve: Resolve | None = None,
    ) -> list[tuple[str, str]]:
        """Every travelling send as a head with a fading tail; report the ones that just landed."""
        landed: list[tuple[str, str]] = []
        alive: list[RouteAnim] = []
        for route_anim in self._route_anims:
            ends = self._ends(route_anim.from_id, route_anim.to_id, resolve)
            path = None if ends is None else send_path(tree_lines, *ends)
            if not path or ends is None or route_anim.step - REGIE_ROUTE_TAIL_CELLS >= len(path):
                continue
            color = self._colors.color(route_anim.from_id)
            for behind in range(REGIE_ROUTE_TAIL_CELLS + 1):
                index = route_anim.step - behind
                if not 0 <= index < len(path):
                    continue
                leaf_index, row_in_leaf = cell_leaf(path[index], tree_lines)
                if 0 <= leaf_index < len(tree_lines):
                    key = tree_lines[leaf_index][2]
                    overlays.setdefault(key, {})[(row_in_leaf, path[index][1])] = (
                        _send_trace_glyph(path, index),
                        _comet_style(color, behind),
                    )
            if route_anim.step == len(path) - 1:
                landed.append((ends[1], color))  # the row that stands for the target
            route_anim.step += 1
            alive.append(route_anim)
        self._route_anims = alive
        self._release_colors()
        return landed
