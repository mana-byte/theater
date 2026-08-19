"""Off-event-loop worker pool for blocking filesystem and subprocess work.

The daemon is a single-process asyncio server. Every ``subprocess.run`` and
every ``ps``/``lsof`` fork on the event loop blocks every RPC handler, every
observer poll, and the lag monitor. This module moves that work onto a
dedicated bounded :class:`~concurrent.futures.ThreadPoolExecutor`.

The pool is dedicated — not the default executor — so a stuck ``git worktree
remove`` cannot starve harness plugins that lean on the default executor for
sub-millisecond sqlite/file reads.

**A callable passed to :func:`to_thread` must never touch ``Store`` or
``Registry`.** The store holds one long-lived SQLite connection touched by
exactly one thread by design. Store access stays on the caller's coroutine.

**Cancellation is honest.** Cancelling the awaiting coroutine stops it from
waiting; it does not stop the underlying thread, which keeps running until
``fn`` returns or its own timeout fires. Do not rely on cancellation to kill
a subprocess.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from theater import timing

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("theater.workers")

MAX_WORKERS = 4

_executor: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor  # noqa: PLW0603
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=MAX_WORKERS,
            thread_name_prefix="theater-worker",
        )
    return _executor


async def to_thread(fn: Callable[..., Any], /, *args: Any, label: str, **kwargs: Any) -> Any:
    """Run ``fn(*args, **kwargs)`` off the event loop, on the dedicated pool.

    *fn* must not touch ``Store`` or ``Registry``. *label* feeds
    ``timing.span(f"workers.{label}")``.
    """
    loop = asyncio.get_running_loop()
    with timing.span(f"workers.{label}", slow_ms=timing.WORKERS_MS):
        return await loop.run_in_executor(_get_executor(), lambda: fn(*args, **kwargs))


async def shutdown() -> None:
    """Drain in-flight workers before the daemon releases its lock.

    Runs ``executor.shutdown(wait=True)`` off the event loop via
    ``run_in_executor``. The outer ``SHUTDOWN_TIMEOUT`` in ``Daemon.run``
    is the only deadline — if it fires, ``_release_files`` runs and the
    lock goes regardless, which is the existing behavior for a stuck
    daemon. No inner timeout: a fallback that releases the executor
    while workers are still running would reintroduce the
    cross-generation mutation race.
    """
    global _executor  # noqa: PLW0603
    if _executor is None:
        return
    exc = _executor
    _executor = None
    await asyncio.get_running_loop().run_in_executor(
        None, lambda: exc.shutdown(wait=True, cancel_futures=True)
    )
