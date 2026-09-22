"""One MCP server process, acting on behalf of exactly one participant.

Identity resolution happens here, once, on the first call. It is deliberately
lazy: the daemon may not exist yet when the harness starts this process, and
failing at import time would show up to the user as "MCP server crashed" with no
explanation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from theater.client import DaemonClient
from theater.mcp.client_pool import DaemonClientPool


@dataclass(slots=True)
class Session:
    """One MCP server process, acting on behalf of exactly one participant."""

    participant_id: str | None
    harness: str
    client: DaemonClient | DaemonClientPool
    _resolved: bool = False
    _identity_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    async def identify(self) -> dict:
        """Announce ourselves to the daemon and cache the resulting record.

        A trusted launch supplies the reserved participant id. Without one the
        daemon records an external-origin participant; addressability can only
        arrive later through an exact provider-backed adoption.
        """
        async with self._identity_lock:
            if self._resolved:
                record = await self.client.call("participants.get", id=self.participant_id)
            else:
                record = await self.client.call(
                    "hello",
                    id=self.participant_id,
                    harness=self.harness,
                    cwd=str(Path.cwd()),
                )
                assert isinstance(record, dict)
                self.participant_id = record["id"]
                self._resolved = True
            assert isinstance(record, dict)
            return record

    async def me(self) -> dict:
        if not self._resolved:
            return await self.identify()
        record = await self.client.call("participants.get", id=self.participant_id)
        assert isinstance(record, dict)
        return record
