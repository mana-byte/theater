"""RPC handler registry and dispatch type.

Handlers register at import via ``method``; ``__init__`` imports every module so
``METHODS`` is complete on a cold import.
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
