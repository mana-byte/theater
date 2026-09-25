"""Reusable footer counter interpolation and pulsing Content.

Pure animation mechanics shared by ``PriceFooter`` and ``StatsFooter``: the
grayscale pulse applied to a value string, and the per-frame integer/float
interpolation that snaps once the remaining change is no longer visible.
The widgets own their timers and reactives; these functions compute frames.
"""

from __future__ import annotations

from collections.abc import Callable

from textual.content import Content

from regie.animations.pulse import working_harness_style


def _pulsing_value(
    value: str,
    *,
    frame: int,
    active: bool,
    value_style: str,
) -> Content:
    """Render one footer value with the tree's working-harness grey wave."""
    if not active:
        return Content.assemble((value, value_style))
    parts: list[str | tuple[str, str]] = []
    offset = 0
    for char in value:
        if char.isspace():
            parts.append(char)
            continue
        parts.append((char, working_harness_style(frame, offset)))
        offset += 1
    return Content.assemble(*parts)


def advance_toward[N: (int, float)](
    value: N, target: N, step: N, formatter: Callable[[N], str]
) -> N:
    """Move one frame toward target, snapping once the remaining change is invisible."""
    candidate = value + step
    if (step >= 0 and candidate >= target) or (step < 0 and candidate <= target):
        return target
    return target if formatter(candidate) == formatter(target) else candidate
