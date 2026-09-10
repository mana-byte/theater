"""Non-blocking participant kills for the régie."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from theater.client import DaemonClient

logger = logging.getLogger("theater.regie.kill")


@dataclass(frozen=True)
class KillResult:
    """Outcome of one kill request."""

    participant_id: str
    ok: bool
    error: str | None = None


type KillCallback = Callable[[KillResult], Awaitable[None]]


class KillController:
    """Run coalesced kills on a dedicated client."""

    def __init__(self, client: DaemonClient) -> None:
        self._client = client
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    @property
    def in_flight(self) -> frozenset[str]:
        """Participant ids with a kill request still running."""
        return frozenset(self._tasks)

    def request(self, participant_id: str, on_done: KillCallback | None = None) -> bool:
        """Start a kill unless one is running or the controller is closed."""
        if self._closed or participant_id in self._tasks:
            return False
        task = asyncio.create_task(
            self._run(participant_id, on_done), name=f"regie-kill-{participant_id}"
        )
        self._tasks[participant_id] = task

        def _forget(task: asyncio.Task[None], pid: str = participant_id) -> None:
            if self._tasks.get(pid) is task:
                del self._tasks[pid]

        task.add_done_callback(_forget)
        return True

    async def _run(self, participant_id: str, on_done: KillCallback | None) -> None:
        try:
            await self._client.call("participant.kill", id=participant_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result = KillResult(participant_id=participant_id, ok=False, error=str(exc))
        else:
            result = KillResult(participant_id=participant_id, ok=True)
        if on_done is None:
            return
        try:
            await on_done(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("kill completion handling failed for %s", participant_id)

    async def aclose(self) -> None:
        """Cancel and drain kills, then close the client."""
        self._closed = True
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()
        with contextlib.suppress(Exception):
            await self._client.aclose()
