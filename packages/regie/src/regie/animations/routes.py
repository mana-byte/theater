"""Tree-route animation state, glyph mechanics, and the route animation controller.

The controller never sees ``RegieApp`` or Textual; the app alone owns timers and
``TreePanel.set_overlays``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from regie.animations.pulse import advance_pulse_frame, working_harness_style
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
    send_path,
)
from regie.ui_constants import (
    REGIE_AWAIT_ANIM_TTL,
    REGIE_MAX_AWAIT_ANIMS,
    REGIE_MAX_TRACE_ANIMS,
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

    Unchanged *glyph* cues the caller to leave the cell alone, not grey an unused line.
    """
    arms = _RAIL_ARMS.get(glyph)
    if arms is None:
        return glyph
    return _AWAIT_TRACE_GLYPHS.get((glyph, directions & arms), glyph)


def _await_route_style(frame: int, offset: int = 0) -> str:
    """The working harness grayscale, at this cell's place along the route.

    No ``bold``: some terminals promote bold grey to bright ANSI, making the line
    brighter than a live agent instead of dimmer.
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

    Returns decisions only; the app performs every Textual side effect.
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
    ) -> TickResult:
        """Compute overlays for the current frame and advance all anims.

        ``highlight_fn`` is passed at call time to preserve the monkeypatch seam in ``regie.app``.
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
            await_anim.frame = advance_pulse_frame(await_anim.frame)

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
