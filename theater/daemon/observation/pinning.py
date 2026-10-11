"""Pin Git off the event loop before an observed completion finishes its job.

The terminal write is synchronous and ``JobManager`` refuses to run Git on the loop, so every
observer path that can end a delivered job holds a pin across it.
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from typing import TYPE_CHECKING, Any

from theater.harness.source import Batch

logger = logging.getLogger("theater.observer")


def batch_may_complete(batch: Batch) -> bool:
    """A turn end in the events, or in the state an attachment is settled from."""
    attached = batch.attached
    return any(event.turn_end for event in batch.events) or (
        attached is not None and attached.last_event is not None and attached.last_event.turn_end
    )


class CompletionPinning:
    if TYPE_CHECKING:
        jobs: Any

    async def _pin_ahead(self, pid: str) -> list[str]:
        """Pin what may complete off the loop; a failure leaves those jobs unpinned."""
        jobs = getattr(self, "jobs", None)
        if jobs is None:
            return []
        try:
            return await jobs.prepare_pins(pid)
        except Exception:
            logger.exception("pinning completion evidence for %s failed", pid)
            return []

    def _release_pins(self, handles: list[str]) -> None:
        jobs = getattr(self, "jobs", None)
        if handles and jobs is not None:
            jobs.release_pins(handles)

    @asynccontextmanager
    async def _pinned(self, pid: str):
        handles = await self._pin_ahead(pid)
        try:
            yield
        finally:
            self._release_pins(handles)

    def _pinned_for(self, pid: str, batch: Batch) -> AbstractAsyncContextManager[None]:
        """Hold a pin only when applying this batch can end a job; other batches cost no Git."""
        return self._pinned(pid) if batch_may_complete(batch) else nullcontext()
