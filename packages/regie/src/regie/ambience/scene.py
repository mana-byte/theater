"""The scene contract every tree ambience implements.

A scene draws into the empty band under the tree's last row. It is told the band's size and
which phase it is in; it returns the cells to draw. Scenes never see the tree or the app.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

#: The longest an intro or outro may run; the tree's focus must never feel laggy.
MAX_TRANSITION_SECONDS = 5.0


class Phase(StrEnum):
    INTRO = "intro"
    IDLE = "idle"
    OUTRO = "outro"


@dataclass(frozen=True, slots=True)
class Cell:
    """One printable ASCII character at ``(x, y)``; ``y`` 0 is the band's top row.

    ``style`` is a Textual style string, so theme variables (``$accent``) resolve per theme.
    No other Unicode or emoji, which render unevenly, bar the half blocks of ``pixels``.
    """

    x: int
    y: int
    glyph: str
    style: str = ""


class Scene:
    """Base for a built-in ambience. Subclasses override the class values and ``frame``.

    Lifecycle: the driver builds a scene with a seeded ``rng``, calls ``resize`` whenever the
    band changes (including before the first frame), then ``frame`` once per tick.
    """

    #: The name selected by ``[regie] tree_ambience``.
    name: ClassVar[str] = ""
    #: Ticks per second while drawing; keep it low, this is ambience, not a game.
    fps: ClassVar[float] = 4.0
    #: The band must have at least this many free rows, or the scene is not drawn at all.
    min_rows: ClassVar[int] = 2
    #: How long the intro and outro take, at most MAX_TRANSITION_SECONDS each.
    intro_seconds: ClassVar[float] = 2.0
    outro_seconds: ClassVar[float] = 2.0

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.width = 0
        self.height = 0

    def resize(self, width: int, height: int) -> None:
        """The band changed size; keep existing state but drop anything now out of bounds."""
        self.width, self.height = width, height

    def frame(self, phase: Phase, progress: float, dt: float) -> list[Cell]:
        """The cells to draw now.

        ``progress`` runs 0 → 1 through an intro or outro (an outro at 1 must draw nothing);
        during idle it is the seconds spent idling. ``dt`` is the time since the last frame.
        Cells outside ``width`` × ``height`` are clipped by the caller.
        """
        raise NotImplementedError


__all__ = ["MAX_TRANSITION_SECONDS", "Cell", "Phase", "Scene"]
