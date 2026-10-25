"""Row-wide sweep that announces a change on one leaf: pure styling and a keyed timer.

``LeafWipeController`` owns timing only; ``apply_wipe`` tints a rendered leaf at one progress.
"""

from __future__ import annotations

import time
from collections.abc import Collection
from dataclasses import dataclass

from rich.cells import cell_len
from textual.content import Content

from regie.render.layout import Key
from regie.ui_constants import (
    REGIE_WIPE_BAND_CELLS,
    REGIE_WIPE_SECONDS,
    REGIE_WIPE_WAKE_CELLS,
)

#: One running sweep as a leaf draws it: progress in [0, 1) and the theme colour it tints with.
type Wipe = tuple[float, str]


def status_wipe_color(previous: str, status: str) -> str | None:
    """The colour announcing a status change, or None when the change is not worth a sweep."""
    if status == "awaiting_input":
        return "$warning"
    if status == "dead":
        return "$error"
    if previous == "working" and status == "idle":
        return "$success"
    return None


def apply_wipe(content: Content, wipe: Wipe, *, width: int | None = None) -> Content:
    """Mark *content* with a reversed block that runs left to right, a bold wake behind it.

    Short rows are padded first: a sweep that stops where the text does reads as a glitch.
    """
    progress, color = wipe
    lines = content.split("\n", allow_blank=True)
    span = max(width or 0, *(cell_len(line.plain) for line in lines))
    if span <= 0:
        return content
    edge = round(progress * (span + REGIE_WIPE_BAND_CELLS + REGIE_WIPE_WAKE_CELLS))
    return Content("\n").join(
        _mark_row(line, span, edge - REGIE_WIPE_BAND_CELLS, edge, color) for line in lines
    )


def _mark_row(row: Content, span: int, band_start: int, edge: int, color: str) -> Content:
    row += " " * (span - cell_len(row.plain))
    end = len(row)
    wake_start = max(0, band_start - REGIE_WIPE_WAKE_CELLS)
    if wake_start < min(band_start, end):
        row = row.stylize(f"bold {color}", wake_start, min(band_start, end))
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


class LeafWipeController:
    """Time one sweep per leaf; starting another on the same leaf replaces it."""

    def __init__(self, *, enabled: bool = True, seconds: float = REGIE_WIPE_SECONDS) -> None:
        self._enabled = enabled
        self._seconds = seconds
        self._sweeps: dict[Key, _Sweep] = {}

    @property
    def active(self) -> bool:
        return bool(self._sweeps)

    def has(self, key: Key) -> bool:
        """Whether a sweep is still running on *key*."""
        return key in self._sweeps

    def start(self, key: Key, color: str, *, now: float | None = None) -> None:
        if self._enabled:
            self._sweeps[key] = _Sweep(color, time.monotonic() if now is None else now)

    def cancel(self, keys: Collection[Key]) -> None:
        for key in keys:
            self._sweeps.pop(key, None)

    def tick(self, *, now: float | None = None) -> WipeFrame:
        current = time.monotonic() if now is None else now
        running: dict[Key, Wipe] = {}
        finished: set[Key] = set()
        for key, sweep in list(self._sweeps.items()):
            progress = (current - sweep.started) / self._seconds
            if progress >= 1:
                del self._sweeps[key]
                finished.add(key)
            else:
                running[key] = (max(0.0, progress), sweep.color)
        return WipeFrame(running, frozenset(finished), self.active)
