"""Braille spinner frame lookup and advancement.

The spinner is the braille character cycling in the status column of a working
participant. The pulse is its grayscale sibling (see :mod:`animations.pulse`);
both share a 10-frame cycle length so the working harness stays in lockstep
with the spinner (see :data:`REGIE_SPINNER_FRAMES` /
:data:`REGIE_WORKING_HARNESS_STYLES`).
"""

from __future__ import annotations

from theater.constants.regie import REGIE_SPINNER_FRAMES as SPINNER_FRAMES

#: Frames in one spinner cycle, derived from the constant so both stay in lockstep.
SPINNER_CYCLE = len(SPINNER_FRAMES)


def spinner_frame(frame: int) -> str:
    """The braille character at *frame*, wrapping at the cycle length."""
    return SPINNER_FRAMES[frame % SPINNER_CYCLE]


def advance_spinner_frame(frame: int) -> int:
    """Advance *frame* by one, wrapping at the spinner cycle length."""
    return (frame + 1) % SPINNER_CYCLE
