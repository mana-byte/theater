"""Reusable cycling state machine for styled dashboard text."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass

from theater.regie.animations.reveal import StyledPart

#: Phases returned by the controller; the widget maps each to a timer delay.
TYPING_IN = "typing_in"
HOLDING = "holding"
TYPING_OUT = "typing_out"

# Cursor blink cadence: toggles each character frame; one on-off cycle = 2 * char_interval.


@dataclass(frozen=True, slots=True)
class CyclingTextFrame:
    """Phase, visible codepoints, delay until the next tick, and cursor state."""

    phase: str
    visible: int
    next_delay: float
    cursor: bool


def _part_len(part: StyledPart) -> int:
    return len(part if isinstance(part, str) else part[0])


def _parts_len(parts: Sequence[StyledPart]) -> int:
    return sum(_part_len(p) for p in parts)


class CyclingTextController:
    """Pure state machine for cycling styled text through type, hold, and erase phases."""

    def __init__(
        self,
        items: Sequence[Sequence[StyledPart]],
        *,
        hold: float = 10.0,
        char_interval: float = 0.1,
        randomize: bool = False,
        rng: random.Random | None = None,
    ) -> None:
        self._items = items
        self._hold = hold
        self._char_interval = char_interval
        self._randomize = randomize
        self._rng = rng or random.Random()
        self._index = self._rng.randrange(len(items)) if randomize and items else 0
        self._phase = TYPING_IN
        self._visible = 0
        self._frame = 0

    @property
    def active(self) -> bool:
        return len(self._items) > 0

    @property
    def parts(self) -> Sequence[StyledPart]:
        return self._items[self._index] if self.active else ()

    @property
    def total_length(self) -> int:
        return _parts_len(self.parts)

    @property
    def index(self) -> int:
        return self._index

    @property
    def visible(self) -> int:
        return self._visible

    @property
    def randomized(self) -> bool:
        return self._randomize

    @property
    def initial_delay(self) -> float:
        """Delay before the first tick; also the per-character typing interval."""
        return self._char_interval

    @property
    def resume_delay(self) -> float:
        """Delay to use after resuming the current phase."""
        return self._hold if self._phase == HOLDING else self._char_interval

    @property
    def phase(self) -> str:
        """Current animation phase."""
        return self._phase

    def advance(self) -> bool:
        """Move to the next item and reset its typing state."""
        if not self.active:
            return False
        self._index = self._next_index()
        self._phase = TYPING_IN
        self._visible = 0
        self._frame = 0
        return True

    def _next_index(self) -> int:
        if not self._randomize:
            return (self._index + 1) % len(self._items)
        if len(self._items) == 1:
            return self._index
        return (self._index + self._rng.randrange(1, len(self._items))) % len(self._items)

    def tick(self) -> CyclingTextFrame:
        """Advance one step and return the resulting frame."""
        if not self.active:
            return CyclingTextFrame(TYPING_IN, 0, self._hold, cursor=False)
        index = self._frame
        self._frame += 1
        blink = index % 2 == 0
        if self._phase == TYPING_IN:
            self._visible += 1
            if self._visible >= self.total_length:
                self._phase = HOLDING
                return CyclingTextFrame(HOLDING, self._visible, self._hold, cursor=False)
            return CyclingTextFrame(TYPING_IN, self._visible, self._char_interval, cursor=blink)
        if self._phase == HOLDING:
            self._phase = TYPING_OUT
            return CyclingTextFrame(TYPING_OUT, self._visible, self._char_interval, cursor=blink)
        # TYPING_OUT
        self._visible -= 1
        if self._visible <= 0:
            self._index = self._next_index()
            self._phase = TYPING_IN
            self._visible = 0
            return CyclingTextFrame(TYPING_IN, 0, self._char_interval, cursor=blink)
        return CyclingTextFrame(TYPING_OUT, self._visible, self._char_interval, cursor=blink)


class CyclingWindowController:
    """Cycle a fixed window while revealing only its incoming item."""

    def __init__(
        self,
        items: Sequence[Sequence[StyledPart]],
        *,
        window_size: int,
        hold: float,
        char_interval: float,
    ) -> None:
        if window_size < 1:
            raise ValueError("window_size must be positive")
        self._items = items
        self._window_size = window_size
        self._hold = hold
        self._char_interval = char_interval
        self._index = 0
        self._phase = HOLDING
        self._visible = self.incoming_length
        self._frame = 0

    @property
    def active(self) -> bool:
        return len(self._items) > 0

    @property
    def index(self) -> int:
        return self._index

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def visible(self) -> int:
        return self._visible

    @property
    def incoming_visible(self) -> int | None:
        return None if self._phase == HOLDING else self._visible

    @property
    def window(self) -> tuple[Sequence[StyledPart], ...]:
        if not self.active:
            return ()
        count = min(self._window_size, len(self._items))
        return tuple(
            self._items[(self._index + offset) % len(self._items)] for offset in range(count)
        )

    @property
    def incoming_length(self) -> int:
        window = self.window
        return _parts_len(window[-1]) if window else 0

    @property
    def initial_delay(self) -> float:
        return self._hold

    @property
    def reveal_delay(self) -> float:
        return self._char_interval

    @property
    def resume_delay(self) -> float:
        return self._hold if self._phase == HOLDING else self._char_interval

    def advance(self) -> bool:
        if not self.active:
            return False
        self._index = (self._index + 1) % len(self._items)
        self._phase = TYPING_IN
        self._visible = 0
        self._frame = 0
        return True

    def tick(self) -> CyclingTextFrame:
        if not self.active:
            return CyclingTextFrame(HOLDING, 0, self._hold, cursor=False)
        if self._phase == HOLDING:
            self.advance()
        blink = self._frame % 2 == 0
        self._frame += 1
        self._visible += 1
        if self._visible >= self.incoming_length:
            self._visible = self.incoming_length
            self._phase = HOLDING
            return CyclingTextFrame(HOLDING, self._visible, self._hold, cursor=False)
        return CyclingTextFrame(TYPING_IN, self._visible, self._char_interval, cursor=blink)
