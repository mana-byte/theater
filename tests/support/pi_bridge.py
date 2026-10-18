"""A scripted Pi frontend peer, the bridge snapshot it answers with, and its fixed identities."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import cast

from theater.harness.builtin.plugins.pi.frontend import (
    PI_FRONTEND_PROTOCOL,
)

PARTICIPANT = "pi-bridge-child"


GENERATION = 4


SESSION_A = "pi-session-a"


SESSION_B = "pi-session-b"


def bridge_snapshot(
    *,
    session_id: str = SESSION_A,
    bridge_epoch: int = 2,
    snapshot_revision: int = 1,
    sequence: int = 0,
    model: str | None = "openai/gpt-5.6",
    thinking: str | None = "high",
    execution_state: str = "idle",
    pending_interaction: dict[str, object] | None = None,
    settings_update: bool = True,
    send: bool = True,
    interrupt: bool = True,
    native_turn_id: str | None = None,
) -> dict[str, object]:
    return {
        "protocol": PI_FRONTEND_PROTOCOL,
        "native_session_id": session_id,
        "bridge_epoch": bridge_epoch,
        "snapshot_revision": snapshot_revision,
        "sequence": sequence,
        "settings": {"model": model, "reasoning_effort": thinking},
        "execution_state": execution_state,
        "native_turn_id": native_turn_id,
        "pending_interaction": pending_interaction,
        "capabilities": {
            "settings_update": settings_update,
            "send": send,
            "interrupt": interrupt,
            "model_update": False,
            "reasoning_effort_update": True,
        },
    }


ResponseHandler = Callable[[Mapping[str, object]], object | Awaitable[object]]


@dataclass
class ScriptedPiPeer:
    responses: dict[str, object | ResponseHandler] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    notifications_queue: asyncio.Queue[object] = field(default_factory=asyncio.Queue)
    closed: bool = False

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        del timeout
        self.requests.append((method, dict(params)))
        response = self.responses.get(method)
        if response is None:
            raise AssertionError(f"unexpected Pi frontend request: {method}")
        value = response(params) if callable(response) else response
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, Mapping)
        return dict(value)

    def notifications(self) -> AsyncIterator[Mapping[str, object]]:
        return self._notifications()

    async def _notifications(self) -> AsyncIterator[Mapping[str, object]]:
        while not self.closed:
            frame = await self.notifications_queue.get()
            if frame is None:
                return
            assert isinstance(frame, Mapping)
            yield cast(Mapping[str, object], frame)

    async def aclose(self) -> None:
        self.closed = True
        self.notifications_queue.put_nowait(None)

    def push(self, frame: Mapping[str, object]) -> None:
        self.notifications_queue.put_nowait(dict(frame))
