"""One MCP server process, acting on behalf of exactly one participant.

Identity resolution happens here, once, on the first call. It is deliberately
lazy: the daemon may not exist yet when the harness starts this process, and
failing at import time would show up to the user as "MCP server crashed" with no
explanation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from theater.client import DaemonClient


@dataclass(slots=True)
class Session:
    """One MCP server process, acting on behalf of exactly one participant."""

    participant_id: str | None
    harness: str
    client: DaemonClient
    _resolved: bool = False

    async def identify(self) -> dict:
        """Announce ourselves to the daemon and cache the resulting record.

        `pane` is read from $TMUX_PANE, which usually is not there: the MCP SDK
        replaces the inherited environment with a six-variable allowlist unless
        the harness config says otherwise. When it is missing and no id was
        given on argv, the daemon files us as External — correct, since without a
        pane nobody can type into us.
        """
        record = await self.client.call(
            "hello",
            id=self.participant_id,
            harness=self.harness,
            pane=os.environ.get("TMUX_PANE"),
            cwd=str(Path.cwd()),
        )
        assert isinstance(record, dict)
        self.participant_id = record["id"]
        self._resolved = True
        return record

    async def me(self) -> dict:
        if not self._resolved:
            return await self.identify()
        record = await self.client.call("participants.get", id=self.participant_id)
        assert isinstance(record, dict)
        return record
