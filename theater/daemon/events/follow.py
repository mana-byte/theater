"""Lost-wakeup-safe waits over durable orchestration journal reads."""

from __future__ import annotations

import asyncio
import contextlib
import math
from collections.abc import Mapping

from theater.daemon.events.reader import JournalReader, StateReadError, StreamCursor
from theater.frontend.capabilities import PUBLIC_LIMITS

_DEFAULT_WAIT_SECONDS = float(PUBLIC_LIMITS["follow_wait_seconds"])
_MAX_WAIT_SECONDS = float(PUBLIC_LIMITS["follow_wait_max_seconds"])
_DEFAULT_LIMIT = int(PUBLIC_LIMITS["entity_page_default"])


class FollowService:
    """Wait for journal commits without trusting an in-memory notification alone."""

    def __init__(self, journal) -> None:
        self._reader = JournalReader(journal)
        self._journal = journal
        self._loop: asyncio.AbstractEventLoop | None = None
        self._revision = 0
        self._waiters: dict[object, asyncio.Event] = {}
        self._closed = False
        journal.register_listener(self._after_commit)

    @property
    def waiter_count(self) -> int:
        return len(self._waiters)

    async def follow(
        self,
        cursor: Mapping[str, object],
        *,
        wait_seconds: object = _DEFAULT_WAIT_SECONDS,
        limit: int = _DEFAULT_LIMIT,
    ) -> dict[str, object]:
        target = _cursor_from_wire(cursor)
        wait = _wait_seconds(wait_seconds)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise StateReadError("bad_request", "state follow limit must be between 1 and 500")
        if self._closed:
            raise StateReadError("resnapshot_required", "the state service is shutting down")
        loop = asyncio.get_running_loop()
        self._bind_loop(loop)
        batch = self._reader.read(target, limit=limit)
        if batch.transactions:
            return _result(batch.transactions, batch.cursor, timed_out=False)
        if wait == 0:
            return _result((), batch.cursor, timed_out=False)
        return await self._wait_for_batch(target, limit=limit, wait=wait, loop=loop)

    async def _wait_for_batch(
        self,
        target: StreamCursor,
        *,
        limit: int,
        wait: float,
        loop: asyncio.AbstractEventLoop,
    ) -> dict[str, object]:
        deadline = loop.time() + wait
        while True:
            observed_revision = self._revision
            # Reading after capturing the notification generation closes the
            # gap between the first empty read and waiter registration.
            batch = self._reader.read(target, limit=limit)
            if batch.transactions:
                return _result(batch.transactions, batch.cursor, timed_out=False)
            if self._closed:
                raise StateReadError("resnapshot_required", "the state service is shutting down")
            if self._revision != observed_revision:
                continue
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._deadline_result(target, limit=limit)
            await self._wait_for_notification(observed_revision, remaining)
            batch = self._reader.read(target, limit=limit)
            if batch.transactions:
                return _result(batch.transactions, batch.cursor, timed_out=False)
            if loop.time() >= deadline:
                return _result((), batch.cursor, timed_out=True)

    async def _wait_for_notification(self, observed_revision: int, remaining: float) -> None:
        event = asyncio.Event()
        token = object()
        self._waiters[token] = event
        if self._revision != observed_revision or self._closed:
            event.set()
        try:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(event.wait(), timeout=remaining)
        finally:
            self._waiters.pop(token, None)

    def _deadline_result(self, target: StreamCursor, *, limit: int) -> dict[str, object]:
        batch = self._reader.read(target, limit=limit)
        return _result(batch.transactions, batch.cursor, timed_out=not bool(batch.transactions))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for waiter in tuple(self._waiters.values()):
            waiter.set()
        await asyncio.sleep(0)

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("state follow service is bound to a different event loop")

    def _after_commit(self, _ending_sequence: int) -> None:
        loop = self._loop
        if self._closed or loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._notify)

    def _notify(self) -> None:
        if self._closed:
            return
        self._revision += 1
        for waiter in tuple(self._waiters.values()):
            waiter.set()


def _cursor_from_wire(value: Mapping[str, object]) -> StreamCursor:
    stream_id = value.get("stream_id")
    sequence = value.get("sequence")
    if not isinstance(stream_id, str) or not stream_id:
        raise StateReadError("bad_request", "state cursor stream_id must be a non-empty string")
    if type(sequence) is not int or sequence < 0:
        raise StateReadError("bad_request", "state cursor sequence must be a non-negative integer")
    return StreamCursor(stream_id, sequence)


def _wait_seconds(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StateReadError("bad_request", "state follow wait_seconds must be finite")
    wait = float(value)
    if not math.isfinite(wait):
        raise StateReadError("bad_request", "state follow wait_seconds must be finite")
    if not 0 <= wait <= _MAX_WAIT_SECONDS:
        raise StateReadError(
            "bad_request",
            f"state follow wait_seconds must be between 0 and {_MAX_WAIT_SECONDS:g}",
        )
    return wait


def _result(
    transactions: tuple[dict[str, object], ...], cursor: StreamCursor, *, timed_out: bool
) -> dict[str, object]:
    return {"transactions": list(transactions), "cursor": cursor.to_wire(), "timed_out": timed_out}


__all__ = ["FollowService"]
