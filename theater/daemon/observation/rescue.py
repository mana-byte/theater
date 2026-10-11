"""Rescue policy: what one capture of a quiet idle prompt may decide about a job."""

from __future__ import annotations

from enum import Enum

from theater.constants.observation import RESCUE_CONFIRM_SECONDS, STALE_OPEN_TURN_SECONDS
from theater.harness.contracts.source import NativeTurnState


class RescueVerdict(Enum):
    """A bare prompt only arms; ending a job takes a later, quiet confirmation."""

    DECLINE = "decline"
    ARM = "arm"
    HOLD = "hold"
    SALVAGE = "salvage"
    UNVERIFIED = "unverified"


def confirmation_due(armed_at: float | None, now: float) -> bool:
    """Whether an armed rescue may capture again; an unarmed one always may."""
    return armed_at is None or now - armed_at >= RESCUE_CONFIRM_SECONDS


def rescue_verdict(
    *, armed_at: float | None, now: float, prompt: bool, turn: NativeTurnState | None
) -> RescueVerdict:
    """Decide from a readable capture; open native work is never salvaged as done."""
    if not prompt:
        return RescueVerdict.DECLINE
    if armed_at is None:
        return RescueVerdict.ARM
    if turn is None or not turn.open:
        return RescueVerdict.SALVAGE
    if now - armed_at < STALE_OPEN_TURN_SECONDS:
        return RescueVerdict.HOLD
    return RescueVerdict.UNVERIFIED
