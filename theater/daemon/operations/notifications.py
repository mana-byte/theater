"""After-commit wakeups whose consumers always re-read durable state."""

from __future__ import annotations

import asyncio


class OperationSubscription:
    def __init__(
        self,
        owner: OperationNotifier,
        operation_id: str,
        future: asyncio.Future[None],
    ) -> None:
        self._owner = owner
        self._operation_id = operation_id
        self._future = future

    async def wait(self) -> None:
        await self._future

    def close(self) -> None:
        self._owner._discard(self._operation_id, self._future)


class OperationNotifier:
    """One-shot subscriptions avoid clearing another waiter's notification."""

    def __init__(self) -> None:
        self._waiters: dict[str, set[asyncio.Future[None]]] = {}

    def subscribe(self, operation_id: str) -> OperationSubscription:
        future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(operation_id, set()).add(future)
        return OperationSubscription(self, operation_id, future)

    def notify(self, operation_id: str) -> None:
        for future in self._waiters.pop(operation_id, set()):
            if not future.done():
                future.set_result(None)

    def _discard(self, operation_id: str, future: asyncio.Future[None]) -> None:
        waiters = self._waiters.get(operation_id)
        if waiters is None:
            return
        waiters.discard(future)
        if not waiters:
            self._waiters.pop(operation_id, None)
        if not future.done():
            future.cancel()


__all__ = ["OperationNotifier", "OperationSubscription"]
