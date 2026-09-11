"""Shared provider access for mutation guards and read-only RPC projections."""

from __future__ import annotations

from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.models import HumanPresent


async def require_absent(daemon, participant_id: str) -> None:
    """Resolve the composed provider at call time; never grant absence without it."""
    provider = getattr(daemon, "presence", None)
    if provider is None:
        raise HumanPresent(
            f"human-presence protection for {participant_id!r} cannot be verified: "
            "the daemon has no composed presence provider, so no mutation may proceed; "
            f"await_sessions(handles=[{participant_id!r}]) and report this configuration"
        )
    await provider.require_absent(participant_id)


def presence_snapshot(daemon, participant_id: str) -> PresenceSnapshot:
    """Project cached focus facts without I/O; missing or failed facts protect."""
    provider = getattr(daemon, "presence", None)
    if provider is None:
        return PresenceSnapshot(PresenceState.UNKNOWN, "presence provider not composed", 0, None)
    try:
        return provider.snapshot(participant_id)
    except Exception:
        return PresenceSnapshot(PresenceState.UNKNOWN, "presence snapshot failed", 0, None)
