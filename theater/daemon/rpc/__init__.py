"""Daemon RPC handler package.

Importing it registers every handler into ``router.METHODS``, the complete wire surface.
"""

from __future__ import annotations

# Handler modules — importing each registers its @method handlers.
from theater.daemon.rpc import (  # noqa: F401
    admin,
    controls,
    hooks,
    interruption,
    jobs,
    management,
    participants,
    plugins,
    recall,
    scratchpad,
    sending,
    skills,
    spawning,
    trajectory,
    transcripts,
    usage,
)
from theater.daemon.rpc.router import METHODS, Handler, method

__all__ = ["METHODS", "Handler", "method"]
