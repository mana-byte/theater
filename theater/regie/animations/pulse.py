"""Shared grayscale frame/style lookups for the working-harness pulse.

The pulse is a grayscale wave that advances one column per frame, shared by
the tree's working-harness display, the await-route style, and the footer
value animation. All call sites import the same lookup so the wave stays
in sync across the régie.
"""

from __future__ import annotations

from theater.constants.regie import REGIE_WORKING_HARNESS_STYLES as WORKING_HARNESS_STYLES

#: Frames in one pulse cycle, matching the spinner cycle (see :mod:`animations.spinner`).
PULSE_CYCLE = len(WORKING_HARNESS_STYLES)


def working_harness_style(frame: int, offset: int = 0) -> str:
    """The grayscale style for a working harness character at *offset*."""
    return WORKING_HARNESS_STYLES[(offset - frame) % PULSE_CYCLE]


def advance_pulse_frame(frame: int) -> int:
    """Advance *frame* by one, wrapping at the pulse cycle length."""
    return (frame + 1) % PULSE_CYCLE
