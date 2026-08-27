"""Mutable runtime channel health with immutable snapshots."""

from __future__ import annotations

from collections import deque

from theater.constants.harness import (
    HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS,
    HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS,
)
from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState

_DIAGNOSTIC_MAX = HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS
_DIAGNOSTIC_CHARS = HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS


def _sanitize_diagnostic(raw: str) -> str:
    text = raw.strip()
    if len(text) > _DIAGNOSTIC_CHARS:
        text = text[:_DIAGNOSTIC_CHARS]
    return text


class ChannelHealthTracker:
    """Per-channel mutable state producing immutable ChannelHealth snapshots."""

    def __init__(self, channel_id: str) -> None:
        self._channel_id = channel_id
        self._state: ChannelHealthState = ChannelHealthState.INACTIVE
        self._dropped: int = 0
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

    def drop(self) -> None:
        self._dropped += 1

    def snapshot(self) -> ChannelHealth:
        return ChannelHealth(
            channel_id=self._channel_id,
            state=self._state,
            diagnostics=tuple(self._diagnostics),
            dropped=self._dropped,
        )

    @property
    def channel_id(self) -> str:
        return self._channel_id


__all__ = ["ChannelHealthTracker"]
