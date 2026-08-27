"""Mutable runtime channel health with immutable snapshots."""

from __future__ import annotations

import math
import re
import time
from collections import deque

from theater.constants.harness import (
    HARNESS_CHANNEL_HEALTH_COUNTER_MAX,
    HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS,
    HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS,
)
from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState

_DIAGNOSTIC_MAX = HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS
_DIAGNOSTIC_CHARS = HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS
_COUNTER_MAX = HARNESS_CHANNEL_HEALTH_COUNTER_MAX
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_TYPE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def _sanitize_diagnostic(raw: str) -> str:
    text = " ".join("".join(char if char.isprintable() else " " for char in raw).split())
    if not text:
        return "channel diagnostic unavailable"
    if len(text) > _DIAGNOSTIC_CHARS:
        text = text[:_DIAGNOSTIC_CHARS]
    return text


def read_error_diagnostic(channel: str, error_code: object) -> str:
    code = (
        error_code
        if isinstance(error_code, str) and _SAFE_ERROR_CODE.fullmatch(error_code)
        else None
    )
    suffix = "" if code is None else f" ({code})"
    return f"{channel} read returned error{suffix}"


def read_exception_diagnostic(prefix: str, exc: BaseException) -> str:
    name = type(exc).__name__
    safe_name = name if _SAFE_TYPE_NAME.fullmatch(name) else "Exception"
    return f"{prefix} ({safe_name})"


class ChannelHealthTracker:
    """Per-channel mutable state producing immutable ChannelHealth snapshots."""

    def __init__(self, channel_id: str) -> None:
        self._channel_id = channel_id
        self._state: ChannelHealthState = ChannelHealthState.INACTIVE
        self._dropped: int = 0
        self._accepted: int = 0
        self._last_success_at: float | None = None
        self._diagnostics: deque[str] = deque(maxlen=_DIAGNOSTIC_MAX)

    def _set(self, state: ChannelHealthState, diagnostic: str | None = None) -> None:
        self._state = state
        if diagnostic is not None:
            self._diagnostics.append(_sanitize_diagnostic(diagnostic))

    def mark_starting(self) -> None:
        self._set(ChannelHealthState.STARTING)

    def mark_healthy(self) -> None:
        self._state = ChannelHealthState.HEALTHY

    def mark_degraded(self, diagnostic: str | None = None) -> None:
        self._set(ChannelHealthState.DEGRADED, diagnostic)

    def mark_failed(self, diagnostic: str | None = None) -> None:
        self._set(ChannelHealthState.FAILED, diagnostic)

    def mark_inactive(self) -> None:
        self._state = ChannelHealthState.INACTIVE

    def drop(self, count: int = 1) -> None:
        if type(count) is not int or count <= 0:
            raise ValueError("channel drop count must be a positive integer")
        self._dropped = min(_COUNTER_MAX, self._dropped + count)

    def record_accepted(self, count: int = 1, *, at: float | None = None) -> None:
        if type(count) is not int or count <= 0:
            raise ValueError("channel accepted count must be a positive integer")
        success_at = _success_at(at)
        self._accepted = min(_COUNTER_MAX, self._accepted + count)
        self._last_success_at = success_at

    def record_success(self, *, at: float | None = None) -> None:
        self._last_success_at = _success_at(at)

    def snapshot(self) -> ChannelHealth:
        return ChannelHealth(
            channel_id=self._channel_id,
            state=self._state,
            diagnostics=tuple(self._diagnostics),
            dropped=self._dropped,
            accepted=self._accepted,
            last_success_at=self._last_success_at,
        )

    @property
    def channel_id(self) -> str:
        return self._channel_id


def _success_at(at: float | None) -> float:
    if at is not None and (type(at) not in (int, float) or not math.isfinite(at) or at < 0):
        raise ValueError("channel success time must be a non-negative finite number")
    return time.time() if at is None else float(at)


__all__ = ["ChannelHealthTracker", "read_error_diagnostic", "read_exception_diagnostic"]
