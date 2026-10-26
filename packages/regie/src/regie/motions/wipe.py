"""Row-wide sweep that announces a change on one leaf: pure styling and a keyed timer.

``LeafWipeController`` owns timing only; ``apply_wipe`` tints a rendered leaf at one progress.
"""

from __future__ import annotations

import time
from collections.abc import Collection
from dataclasses import dataclass

from rich.cells import cell_len
from textual.content import Content

from regie.marks import MarkKind
from regie.render.layout import Key
from regie.ui_constants import (
    REGIE_WIPE_BAND_CELLS,
    REGIE_WIPE_DEATH_SECONDS,
    REGIE_WIPE_SECONDS,
    REGIE_WIPE_TRAIL_ALPHA,
    REGIE_WIPE_WAKE_CELLS,
)

#: One running sweep as a leaf draws it: progress in [0, 1) and the theme colour it tints with.
type Wipe = tuple[float, str]


#: What each event sweeps its row with: the theme colour, and seconds (None: the default length).
#: Marks arriving are keyed by their kind; colour alone does not decide the length.
SWEEPS: dict[str, tuple[str, float | None]] = {
    "spawn_opening": ("$secondary", None),  # the row that asks for the directory opens
    "launched": ("$success", None),  # a spawn's launching mark cleared and its agent is up
    "killed_unseen": ("$error", REGIE_WIPE_DEATH_SECONDS),  # a kill too quick for its mark
    "awaiting_input": ("$warning", None),
    "dead": ("$error", REGIE_WIPE_DEATH_SECONDS),
    "finished": ("$success", None),  # a working turn went idle
    MarkKind.TERMINATING: ("$error", REGIE_WIPE_DEATH_SECONDS),
    MarkKind.SENT: ("$success", None),
    MarkKind.BLOCKED: ("$warning", None),
    MarkKind.UNCERTAIN: ("$warning", None),
    MarkKind.FAILED: ("$error", None),
}


def status_sweep(previous: str, status: str) -> tuple[str, float | None] | None:
    """The sweep announcing a status change, or None when the change is not worth one."""
    if status in {"awaiting_input", "dead"}:
        return SWEEPS[status]
    if previous == "working" and status == "idle":
        return SWEEPS["finished"]
    return None


def apply_wipe(
    content: Content, wipe: Wipe, *, width: int | None = None, blend: bool = False
) -> Content:
    """Mark *content* with a reversed block that runs left to right, a fading trail behind it.

    The trail is blocks of fading opacity: background tints when the theme can *blend*, shade
    glyphs over blanks (and fading text weight) when it cannot. Short rows are padded first: a
    sweep that stops where the text does reads as a glitch.
    """
    progress, color = wipe
    lines = content.split("\n", allow_blank=True)
    span = max(width or 0, *(cell_len(line.plain) for line in lines))
    if span <= 0:
        return content
    edge = round(progress * (span + REGIE_WIPE_BAND_CELLS + REGIE_WIPE_WAKE_CELLS))
    return Content("\n").join(
        _mark_row(line, span, edge - REGIE_WIPE_BAND_CELLS, edge, color, blend) for line in lines
    )


#: Away from the block the trail thins in three tiers: shade glyphs over blanks, and the weight
#: of the text under it, where a tint cannot be drawn.
_TRAIL_SHADES = "▓▒░"
_TRAIL_WEIGHTS = ("bold {}", "{}", "dim {}")


def _fade_trail(row: Content, band_start: int, color: str, blend: bool) -> Content:
    for behind in range(REGIE_WIPE_WAKE_CELLS):
        col = band_start - 1 - behind
        if not 0 <= col < len(row):
            continue
        if blend:
            alpha = (
                REGIE_WIPE_TRAIL_ALPHA * (REGIE_WIPE_WAKE_CELLS - behind) // REGIE_WIPE_WAKE_CELLS
            )
            row = row.stylize(f"on {color} {alpha}%", col, col + 1)
            continue
        tier = behind * len(_TRAIL_SHADES) // REGIE_WIPE_WAKE_CELLS
        if row.plain[col] == " ":
            plain = row.plain
            row = Content(plain[:col] + _TRAIL_SHADES[tier] + plain[col + 1 :], list(row.spans))
            row = row.stylize(color, col, col + 1)
        else:
            row = row.stylize(_TRAIL_WEIGHTS[tier].format(color), col, col + 1)
    return row


def _mark_row(
    row: Content, span: int, band_start: int, edge: int, color: str, blend: bool
) -> Content:
    row += " " * (span - cell_len(row.plain))
    end = len(row)
    row = _fade_trail(row, band_start, color, blend)
    if (start := max(0, band_start)) < min(edge, end):
        row = row.stylize(f"reverse {color}", start, min(edge, end))
    return row


@dataclass(frozen=True, slots=True)
class WipeFrame:
    """Progress of every running sweep, the keys that just ended, and whether any remain."""

    running: dict[Key, Wipe]
    finished: frozenset[Key]
    active: bool


@dataclass(frozen=True, slots=True)
class _Sweep:
    color: str
    started: float
    seconds: float


class LeafWipeController:
    """Time one sweep per leaf; starting another on the same leaf replaces it."""

    def __init__(self, *, enabled: bool = True, seconds: float = REGIE_WIPE_SECONDS) -> None:
        self._enabled = enabled
        self._seconds = seconds  # a sweep's length unless its start names one
        self._sweeps: dict[Key, _Sweep] = {}

    @property
    def active(self) -> bool:
        return bool(self._sweeps)

    def has(self, key: Key) -> bool:
        """Whether a sweep is still running on *key*."""
        return key in self._sweeps

    def start(
        self, key: Key, color: str, *, seconds: float | None = None, now: float | None = None
    ) -> None:
        if self._enabled:
            self._sweeps[key] = _Sweep(
                color,
                time.monotonic() if now is None else now,
                self._seconds if seconds is None else seconds,
            )

    def cancel(self, keys: Collection[Key]) -> None:
        for key in keys:
            self._sweeps.pop(key, None)

    def tick(self, *, now: float | None = None) -> WipeFrame:
        current = time.monotonic() if now is None else now
        running: dict[Key, Wipe] = {}
        finished: set[Key] = set()
        for key, sweep in list(self._sweeps.items()):
            progress = (current - sweep.started) / sweep.seconds
            if progress >= 1:
                del self._sweeps[key]
                finished.add(key)
            else:
                running[key] = (max(0.0, progress), sweep.color)
        return WipeFrame(running, frozenset(finished), self.active)
