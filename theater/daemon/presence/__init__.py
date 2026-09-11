"""Shared daemon human-presence contract plus the one monitor that implements it."""

from theater.daemon.presence.contracts import PresenceProvider, PresenceSnapshot, PresenceState
from theater.daemon.presence.monitor import PresenceMonitor

__all__ = ["PresenceMonitor", "PresenceProvider", "PresenceSnapshot", "PresenceState"]
