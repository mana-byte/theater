"""Termination action adapter using the durable public operation facade."""

from __future__ import annotations

from regie.controllers.actions import ActionRecord, OperationController
from theater.frontend import FrontendClient


class KillController:
    """A narrow public termination controller; it never destroys terminals directly."""

    def __init__(self, client: FrontendClient) -> None:
        self._operations = OperationController(client)

    async def request(self, participant_id: str) -> ActionRecord:
        return await self._operations.terminate(participant_id)

    async def close(self) -> None:
        await self._operations.close()


__all__ = ["KillController"]
