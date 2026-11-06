"""Claude native bridge values and strict wire decoding."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

from theater.constants.harness import HARNESS_RUNTIME_ERROR_MAX_CHARS, HARNESS_RUNTIME_ID_MAX_CHARS
from theater.harness.contracts.runtime import (
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeExecutionState,
)

CHANNEL_ID = "native-live"
PROTOCOL = "claude-frontend-v1"
CONTROL_TIMEOUT_SECONDS = 5.0
MAX_PENDING_OUTCOMES = 32


def text(value: object, label: str, *, limit: int = HARNESS_RUNTIME_ID_MAX_CHARS) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Claude bridge {label} must be a bounded non-blank string")
    return value


def optional_text(value: object, label: str) -> str | None:
    return None if value is None else text(value, label, limit=HARNESS_RUNTIME_ERROR_MAX_CHARS)


def integer(value: object, label: str, *, minimum: int = 0) -> int:
    if type(value) is not int or not minimum <= value < 2**53:
        raise ValueError(f"Claude bridge {label} must be a bounded integer")
    return value


@dataclass(frozen=True, slots=True)
class Snapshot:
    session_id: str
    current_session_id: str
    generation: int
    revision: int
    active_turn_id: str | None
    state: RuntimeExecutionState
    version: str
    error: str | None = None


def snapshot(data: Mapping[str, object]) -> Snapshot:
    if data.get("protocol") != PROTOCOL:
        raise ValueError("Claude bridge protocol is incompatible")
    active = data.get("active_turn_id")
    state = RuntimeExecutionState(str(data.get("state")))
    if state is RuntimeExecutionState.IDLE and active is not None:
        raise ValueError("Claude bridge reported idle with an active turn")
    error = data.get("error")
    return Snapshot(
        session_id=text(data.get("session_id"), "session id"),
        current_session_id=text(
            data.get("current_session_id", data.get("session_id")), "current session id"
        ),
        generation=integer(data.get("generation"), "generation", minimum=1),
        revision=integer(data.get("revision"), "revision"),
        active_turn_id=None if active is None else text(active, "active turn id"),
        state=state,
        version=text(data.get("version"), "version"),
        error=optional_text(error, "error"),
    )


def outcome(data: Mapping[str, object], *, connected_at: float) -> NativeTurnOutcome:
    completed_at = data.get("completed_at")
    if (
        isinstance(completed_at, bool)
        or not isinstance(completed_at, (int, float))
        or not math.isfinite(completed_at)
    ):
        raise ValueError("Claude terminal evidence needs its native completion time")
    result = data.get("result")
    if result is not None and not isinstance(result, str):
        raise TypeError("Claude native result must be text or null")
    return NativeTurnOutcome(
        native_session_id=text(data.get("session_id"), "outcome session"),
        native_turn_id=text(data.get("turn_id"), "outcome turn"),
        terminal=NativeTurnTerminal(str(data.get("terminal"))),
        result=result,
        completeness=(
            ResultCompleteness.COMPLETE
            if isinstance(data.get("result"), str)
            else ResultCompleteness.UNAVAILABLE
        ),
        provenance=ResultProvenance.NATIVE_EVIDENCE,
        error_code=optional_text(data.get("error_code"), "outcome error code"),
        error=optional_text(data.get("error"), "outcome error"),
        from_history=completed_at < connected_at,
        completed_at=completed_at,
    )
