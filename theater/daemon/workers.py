"""Dedicated bounded thread pool for blocking filesystem and subprocess work off the event loop.
Dedicated so a stuck ``git worktree remove`` cannot starve the default executor. Callables must
never touch ``Store``/``Registry`` (one-thread SQLite); cancellation stops waiting, not the thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from theater import timing
from theater.observability.catalog import WORKER_EXECUTION, WORKER_TASK, WORKER_WAIT

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("theater.workers")

MAX_WORKERS = 4

_executor: ThreadPoolExecutor | None = None
_admissions: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = WeakKeyDictionary()


def _get_executor() -> ThreadPoolExecutor:
    global _executor  # noqa: PLW0603
    if _executor is None:
        _admissions.clear()
        _executor = ThreadPoolExecutor(
            max_workers=MAX_WORKERS,
            thread_name_prefix="theater-worker",
        )
    return _executor


async def to_thread(fn: Callable[..., Any], /, *args: Any, label: str, **kwargs: Any) -> Any:
    """Run ``fn`` off the event loop on the dedicated pool; it must not touch ``Store`` or
    ``Registry``.
    """
    loop = asyncio.get_running_loop()
    with timing.span(WORKER_TASK, label=label):
        ctx = contextvars.copy_context()
        executor = _get_executor()
        slots = _admissions.setdefault(loop, asyncio.Semaphore(MAX_WORKERS))
        with timing.span(WORKER_WAIT, label=label):
            await slots.acquire()

        def execute():
            with timing.span(WORKER_EXECUTION, label=label):
                return fn(*args, **kwargs)

        def release(_future):
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(slots.release)

        try:
            future = executor.submit(ctx.run, execute)
        except BaseException:
            slots.release()
            raise
        # Cancellation detaches a caller; capacity belongs to the actual worker.
        future.add_done_callback(release)
        return await asyncio.wrap_future(future)


async def shutdown() -> None:
    """Drain in-flight workers before the daemon releases its lock.
    No inner timeout (``Daemon.run``'s is the only deadline): releasing early would reintroduce the
    cross-generation mutation race.
    """
    global _executor  # noqa: PLW0603
    if _executor is None:
        return
    exc = _executor
    _executor = None
    _admissions.clear()
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: exc.shutdown(wait=True, cancel_futures=True)
    )
