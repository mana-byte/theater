"""Tree-route animation state and mechanics.

Holds the two animation state objects (``RouteAnim``, ``AwaitRouteAnim``),
the ``LeafOverlay`` type alias, the glyph/style lookups, and the three
free functions that compute a send trace glyph, an await route glyph, and
an await route style from the current animation frame.

Extracted from ``theater.regie.app`` with no behavior change. ``RegieApp``
owns the animation timer, the lists of active anims, and the tick that
drives them; everything that is pure data or pure lookup lives here.
"""

from __future__ import annotations

import time

from theater.constants.regie import REGIE_AWAIT_ANIM_TTL
from theater.regie.render.glyphs import (
    LeafCell,
    OverlayGlyph,
    working_harness_style,
)
from theater.regie.render.routing import Cell, Direction

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
