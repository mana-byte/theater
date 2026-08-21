"""Tree-route animation state, mechanics, and controller.

Holds the two animation state objects (``RouteAnim``, ``AwaitRouteAnim``),
the ``LeafOverlay`` type alias, the glyph/style lookups, the three free
functions that compute a send trace glyph, an await route glyph, and an
await route style from the current animation frame, and the
``RouteAnimationController`` that owns route/await collections, TTL reaping,
revision cache, and start/stop/tick decisions.

The controller never receives ``RegieApp`` or Textual. The app alone owns
``Timer``/``set_interval``/``TreePanel.set_overlays`` and keeps thin
compatibility methods.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from theater.constants.regie import (
    REGIE_AWAIT_ANIM_TTL,
    REGIE_MAX_AWAIT_ANIMS,
    REGIE_MAX_TRACE_ANIMS,
)
from theater.regie.render.glyphs import (
    LeafCell,
    OverlayGlyph,
    working_harness_style,
)
from theater.regie.render.layout import Key
from theater.regie.render.routing import (
    AwaitCell,
    Cell,
    Direction,
    await_path,
    cell_leaf,
    send_path,
)

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

#: Which arms each rail glyph the tree draws actually has.
_RAIL_ARMS: dict[str, frozenset[Direction]] = {
    "│": frozenset({(-1, 0), (1, 0)}),
    "─": frozenset({(0, -1), (0, 1)}),
    "└": frozenset({(-1, 0), (0, 1)}),
    "├": frozenset({(-1, 0), (1, 0), (0, 1)}),
}

#: The heavy form of each rail glyph by which of its arms the await route uses.
_AWAIT_TRACE_GLYPHS: dict[tuple[str, frozenset[Direction]], str] = {
    ("│", frozenset({(-1, 0), (1, 0)})): "┃",
    ("│", frozenset({(-1, 0)})): "╿",
    ("│", frozenset({(1, 0)})): "╽",
    ("─", frozenset({(0, -1), (0, 1)})): "━",
    ("─", frozenset({(0, -1)})): "╾",
    ("─", frozenset({(0, 1)})): "╼",
    ("└", frozenset({(-1, 0), (0, 1)})): "┗",
    ("└", frozenset({(-1, 0)})): "┖",
    ("└", frozenset({(0, 1)})): "┕",
    ("├", frozenset({(-1, 0), (1, 0), (0, 1)})): "┣",
    ("├", frozenset({(-1, 0), (1, 0)})): "┠",
    ("├", frozenset({(-1, 0), (0, 1)})): "┡",
    ("├", frozenset({(1, 0), (0, 1)})): "┢",
    ("├", frozenset({(-1, 0)})): "┞",
    ("├", frozenset({(1, 0)})): "┟",
    ("├", frozenset({(0, 1)})): "┝",
}


class RouteAnim:
    """One trace travelling from a sender's leaf to its target's.

    Holds the two participant ids and how many route cells it has travelled —
    never the route itself. The tree refreshes underneath it every second, and
    a stored route would go stale the moment an agent above it dies and every
    row shifts up. Recomputing per frame means the trace lands somewhere
    sensible even if the path changed length, and disappears cleanly the moment
    either end stops being visible.
    """

    def __init__(self, from_id: str, to_id: str) -> None:
        self.from_id = from_id
        self.to_id = to_id
        self.step = 0


class AwaitRouteAnim:
    """One active await relationship pulsing along a visible tree route.

    Carries its own deadline. The pulse is supposed to end on a matching
    ``job.await.end`` row, and mostly does — but a row that never arrives
    would otherwise leave it pulsing for the rest of the session, so it also
    expires on its own after :data:`REGIE_AWAIT_ANIM_TTL`.
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


def _send_trace_glyph(path: list[Cell], index: int) -> str:
    """A heavy line glyph matching how the route passes through *index*."""
    row, col = path[index]
    directions: set[Direction] = set()
    for neighbor_index in (index - 1, index + 1):
        if 0 <= neighbor_index < len(path):
            next_row, next_col = path[neighbor_index]
            directions.add((next_row - row, next_col - col))
    return _SEND_TRACE_GLYPHS.get(frozenset(directions), "━")


def _await_route_glyph(glyph: str, directions: frozenset[Direction]) -> str:
    """*glyph* with the arms the route uses drawn heavy, the rest left light.

    Returns *glyph* unchanged when the route touches none of its arms, which
    is the caller's cue to leave that cell alone rather than grey out a line
    the await does not use.
    """
    arms = _RAIL_ARMS.get(glyph)
    if arms is None:
        return glyph
    return _AWAIT_TRACE_GLYPHS.get((glyph, directions & arms), glyph)


def _await_route_style(frame: int, offset: int = 0) -> str:
    """The working harness grayscale, at this cell's place along the route.

    No ``bold``: the glyphs are already the heavy box-drawing forms, and bold
    promotes a grey into the bright ANSI palette on some terminals — which
    turns the one thing this style is for, a line dimmer than a live agent,
    into a line brighter than one.
    """
    return working_harness_style(frame, offset)


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


class RouteAnimationController:
    """Owns route/await collections, TTL reaping, and revision cache.

    The app constructs this, then calls ``start_route``, ``start_await``,
    ``stop_await``, and ``tick`` to get decisions. The app performs all
    Textual side effects: ``Timer``, ``set_interval``, ``TreePanel.set_overlays``.
    """

    def __init__(self) -> None:
        self._route_anims: list[RouteAnim] = []
        self._await_anims: dict[tuple[str, str, str, str], AwaitRouteAnim] = {}
        self._await_cells: dict[tuple[str, str], list[AwaitCell] | None] = {}
        self._await_cells_revision: int = -1

    @property
    def route_anims(self) -> list[RouteAnim]:
        return self._route_anims

    @property
    def await_anims(self) -> dict[tuple[str, str, str, str], AwaitRouteAnim]:
        return self._await_anims

    def has_active(self) -> bool:
        """Whether any animation is in flight."""
        return bool(self._route_anims or self._await_anims)

    def start_route(
        self,
        tree_lines: list[tuple],
        from_id: str | None,
        to_id: str | None,
    ) -> StartRouteDecision:
        """Begin a trace, if a route exists and the cap has not been reached."""
        if len(self._route_anims) >= REGIE_MAX_TRACE_ANIMS:
            return StartRouteDecision(started=False)
        if send_path(tree_lines, from_id, to_id) is None:
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
    ) -> StartAwaitDecision:
        """Begin an await pulse, reaping expired ones first."""
        self._reap_await_anims()
        if len(self._await_anims) >= REGIE_MAX_AWAIT_ANIMS:
            return StartAwaitDecision(started=False)
        if not token or not handle or not from_id or not to_id or from_id == to_id:
            return StartAwaitDecision(started=False)
        if await_path(tree_lines, from_id, to_id) is None:
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
        if self._route_anims or self._await_anims:
            return StopAwaitDecision(stopped=True)
        return StopAwaitDecision(stopped=True, clear_overlays=True, stop_timer=True)

    def _reap_await_anims(self) -> None:
        """Drop pulses whose end row never arrived."""
        now = time.monotonic()
        for key, anim in list(self._await_anims.items()):
            if anim.expired(now):
                del self._await_anims[key]

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
        monkeypatch seam in ``theater.regie.app``.
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
    ) -> TickResult:
        """Compute overlays for the current frame and advance all anims.

        Returns the overlay map to apply and whether the timer should stop.
        ``highlight_fn`` is resolved/passed at call time to preserve the
        monkeypatch seam in ``theater.regie.app``.
        """
        self._reap_await_anims()
        overlays: dict[Key, LeafOverlay] = {}
        for await_anim in self._await_anims.values():
            for await_cell in (
                self._await_route_cells(
                    tree_lines,
                    tree_revision,
                    await_anim.from_id,
                    await_anim.to_id,
                    highlight_fn,
                )
                or ()
            ):
                col = await_cell.cell[1]
                leaf_index, row_in_leaf = cell_leaf(await_cell.cell)
                if not 0 <= leaf_index < len(tree_lines):
                    continue
                heavy = _await_route_glyph(await_cell.glyph, await_cell.directions)
                if heavy == await_cell.glyph:
                    continue
                key = tree_lines[leaf_index][2]
                overlays.setdefault(key, {})[(row_in_leaf, col)] = (
                    heavy,
                    _await_route_style(await_anim.frame, await_cell.offset),
                )
            await_anim.frame = (await_anim.frame + 1) % 10

        alive: list[RouteAnim] = []
        for route_anim in self._route_anims:
            path = send_path(tree_lines, route_anim.from_id, route_anim.to_id)
            if not path or route_anim.step >= len(path):
                continue
            cell = path[route_anim.step]
            leaf_index, row_in_leaf = cell_leaf(cell)
            if not 0 <= leaf_index < len(tree_lines):
                continue
            key = tree_lines[leaf_index][2]
            overlays.setdefault(key, {})[(row_in_leaf, cell[1])] = _send_trace_glyph(
                path, route_anim.step
            )
            route_anim.step += 1
            alive.append(route_anim)
        self._route_anims = alive
        stop_timer = not self._route_anims and not self._await_anims
        return TickResult(overlays=overlays, stop_timer=stop_timer)
