"""Pure footer counter interpolation and pulse, shared by ``PriceFooter`` and ``StatsFooter``.

Interpolation snaps once the remaining change is no longer visible; widgets own timers.
"""

from __future__ import annotations

from collections.abc import Callable

from textual.content import Content

from regie.animations.pulse import advance_pulse_frame, working_harness_style
from regie.ui_constants import REGIE_FOOTER_ANIM_FRAMES

type StyledParts = list[str | tuple[str, str]]


def pulsing_parts(value: str, *, frame: int, active: bool, value_style: str) -> StyledParts:
    """One counter value as styled parts, with the tree's working-harness grey wave."""
    if not active:
        return [(value, value_style)]
    parts: StyledParts = []
    offset = 0
    for char in value:
        if char.isspace():
            parts.append(char)
            continue
        parts.append((char, working_harness_style(frame, offset)))
        offset += 1
    return parts


def _pulsing_value(
    value: str,
    *,
    frame: int,
    active: bool,
    value_style: str,
) -> Content:
    """Render one footer value with the tree's working-harness grey wave."""
    return Content.assemble(
        *pulsing_parts(value, frame=frame, active=active, value_style=value_style)
    )


def advance_toward[N: (int, float)](
    value: N, target: N, step: N, formatter: Callable[[N], str]
) -> N:
    """Move one frame toward target, snapping once the remaining change is invisible."""
    candidate = value + step
    if (step >= 0 and candidate >= target) or (step < 0 and candidate <= target):
        return target
    return target if formatter(candidate) == formatter(target) else candidate


class CountingValue:
    """One value that counts toward its latest target the way footer counters do.

    Owners drive ``tick`` from their own timer while ``active``; ``snap`` skips the count.
    """

    def __init__(self, formatter: Callable[[float], str]) -> None:
        self._formatter = formatter
        self.display: float | None = None
        self._target: float | None = None
        self._step = 0.0
        self.frame = 0

    @property
    def active(self) -> bool:
        return (
            self.display is not None
            and self._target is not None
            and self._formatter(self.display) != self._formatter(self._target)
        )

    def set_target(self, target: float | None, *, animate: bool) -> bool:
        """Adopt a new target; True when a count is now running."""
        self._target = target
        if target is None or self.display is None or not animate:
            self.snap()
            return False
        self._step = (target - self.display) / REGIE_FOOTER_ANIM_FRAMES
        self.frame = 0
        return self.active

    def snap(self) -> None:
        self.display = self._target

    def tick(self) -> bool:
        """Advance one frame; True while the count is still running."""
        if self.display is None or self._target is None:
            return False
        self.frame = advance_pulse_frame(self.frame)
        self.display = advance_toward(self.display, self._target, self._step, self._formatter)
        return self.active

    def parts(self, *, value_style: str) -> StyledParts | None:
        if self.display is None:
            return None
        return pulsing_parts(
            self._formatter(self.display),
            frame=self.frame,
            active=self.active,
            value_style=value_style,
        )
