"""Participant kills that never stall the régie.

``participant.kill`` can be slow on the daemon side — session teardown,
worktree cleanup — and a client holds its request lock until the reply
arrives. A key action that awaits the kill freezes the régie's message
loop behind it, and running it on the shared client stalls every poll
behind the same lock. The app therefore owns one dedicated client for
kills and this controller runs each request as a background task: the
action returns immediately, repeated presses are coalesced per
participant while one is in flight, and completion reports back on the
event loop. Cancelling on shutdown is the régie leaving, not a change of
mind — the daemon may still finish a request already sent.
"""

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
    """The outcome of one kill request, delivered when its task completes."""

    participant_id: str
    ok: bool
    error: str | None = None


#: Reacts to a finished kill on the event loop; must not raise on a dead app.
type KillCallback = Callable[[KillResult], Awaitable[None]]


class KillController:
    """Fire-and-forget kills on a client the app dedicates to them.

    One in-flight task per participant id: a second press while the first
    is still running is coalesced, and a press after completion starts a
    fresh request. Callback failures are logged, never raised into the
    task — a notification or refresh racing app shutdown must not spawn
    "task exception was never retrieved" noise.
    """

    def __init__(self, client: DaemonClient) -> None:
        self._client = client
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    @property
    def in_flight(self) -> frozenset[str]:
        """Participant ids with a kill request still running."""
        return frozenset(self._tasks)

    def request(self, participant_id: str, on_done: KillCallback | None = None) -> bool:
        """Start a kill for ``participant_id`` unless one is already running.

        Returns whether a new request was started; ``False`` means the
        press was coalesced (one is in flight) or the controller is closed.
        """
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
        """Cancel in-flight kills and close the dedicated client.

        The tasks are awaited so the loop never reports them destroyed
        while pending, and the client close is best effort — unmount must
        not hang on a stuck reply.
        """
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
