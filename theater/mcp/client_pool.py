"""Bounded independent connections for connection-stateless private MCP RPCs."""

from __future__ import annotations

import asyncio

from theater.client import DaemonClient
from theater.constants.harness import MCP_RPC_CONNECTIONS
from theater.observability.catalog import RPC_POOL_WAIT
from theater.observability.engine import span


class DaemonClientPool:
    """Keep one aligned exchange per socket without serializing unrelated tools."""

    def __init__(self, *, size: int = MCP_RPC_CONNECTIONS, autostart: bool = True):
        if size < 1:
            raise ValueError("MCP RPC connection pool size must be positive")
        self._clients = tuple(DaemonClient(autostart=autostart) for _ in range(size))
        self._idle = list(self._clients)
        self._condition = asyncio.Condition()
        self._closed = False

    async def call(self, method: str, **params) -> object:
        with span(RPC_POOL_WAIT, method=method):
            async with self._condition:
                await self._condition.wait_for(lambda: self._closed or bool(self._idle))
                if self._closed:
                    raise RuntimeError("MCP RPC connection pool is closed")
                client = self._idle.pop()
        try:
            return await client.call(method, **params)
        finally:
            async with self._condition:
                self._idle.append(client)
                self._condition.notify_all()

    async def aclose(self) -> None:
        """Reject new work, drain admitted calls, then close every owned socket."""
        async with self._condition:
            self._closed = True
            self._condition.notify_all()
            await self._condition.wait_for(lambda: len(self._idle) == len(self._clients))
        await asyncio.gather(*(client.aclose() for client in self._clients))
