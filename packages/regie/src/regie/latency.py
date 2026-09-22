"""Phase measurements independent of UI state and process logging setup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterator
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from regie.controllers.actions import ActionRecord

logger = logging.getLogger(__name__)


def action_phase(record: ActionRecord, phase: str) -> contextlib.AbstractContextManager[None]:
    """Measure one phase without changing its result, exception, or cancellation."""
    return _phase(f"action.{record.action}.{phase}", f" operation={record.operation_id}")


def startup_phase(phase: str) -> contextlib.AbstractContextManager[None]:
    return _phase(f"startup.{phase}")


async def startup_stage[T](phase: str, work: Callable[[], Awaitable[T]]) -> T:
    with startup_phase(phase):
        return await work()


def startup_milestone(phase: str, started_at: float) -> None:
    """Milestones start at app construction, excluding CLI preflight and imports."""
    with contextlib.suppress(Exception):
        logger.info("startup.%s %.1fms", phase, (monotonic() - started_at) * 1000)


@contextlib.contextmanager
def _phase(name: str, context: str = "") -> Iterator[None]:
    started: float | None = None
    with contextlib.suppress(Exception):
        started = monotonic()
    result = "success"
    try:
        yield
    except BaseException as exc:
        result = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
        raise
    finally:
        if started is not None:
            with contextlib.suppress(Exception):
                logger.info(
                    "%s %.1fms%s result=%s",
                    name,
                    (monotonic() - started) * 1000,
                    context,
                    result,
                )
