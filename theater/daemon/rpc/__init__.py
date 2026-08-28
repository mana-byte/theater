"""Daemon RPC handler package.

Importing this package registers every RPC handler as a side effect, populating
``router.METHODS`` with the complete 33-method wire surface.  The ``METHODS``
dict is the public surface; ``server.py`` reads it for dispatch.
"""

from __future__ import annotations

# Handler modules — importing each registers its @method handlers.
from theater.daemon.rpc import (  # noqa: F401
    admin,
    hooks,
    jobs,
    participants,
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
