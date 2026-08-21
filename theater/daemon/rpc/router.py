"""RPC handler registry and dispatch type.

The ``METHODS`` dict maps wire method names to async handler callables.
Each handler module registers its handlers at import time via the
``method`` decorator.  ``__init__.py`` imports every handler module so
a single ``from theater.daemon.rpc import METHODS`` is the complete
cold-import registration surface.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from theater.daemon.server import Daemon

Handler = Callable[["Daemon", dict[str, Any]], Awaitable[Any]]

METHODS: dict[str, Handler] = {}


def method(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        METHODS[name] = fn
        return fn

    return register
