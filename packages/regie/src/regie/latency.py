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


class StartupTrace:
    """Buffer preflight timings until the launcher's own log is ready."""

    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self._pending: list[tuple[str, float, str]] = []
        self._active = False

    def record(self, phase: str, started_at: float, result: str = "success") -> None:
        with contextlib.suppress(Exception):
            elapsed = (monotonic() - started_at) * 1000
            if self._active:
                logger.info("startup.%s %.1fms result=%s", phase, elapsed, result)
            else:
                self._pending.append((phase, elapsed, result))

    @contextlib.contextmanager
    def phase(self, phase: str) -> Iterator[None]:
        started_at = None
        with contextlib.suppress(Exception):
            started_at = monotonic()
        result = "success"
        try:
            yield
        except BaseException as exc:
            result = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            raise
        finally:
            if started_at is not None:
                self.record(phase, started_at, result)

    def activate(self) -> None:
        self._active = True
        pending, self._pending = self._pending, []
        for phase, elapsed, result in pending:
            with contextlib.suppress(Exception):
                logger.info("startup.%s %.1fms result=%s", phase, elapsed, result)


def action_phase(record: ActionRecord, phase: str) -> contextlib.AbstractContextManager[None]:
    """Measure one phase without changing its result, exception, or cancellation."""
    return _phase(f"action.{record.action}.{phase}", f" operation={record.operation_id}")


def startup_phase(phase: str) -> contextlib.AbstractContextManager[None]:
    return _phase(f"startup.{phase}")


def presentation_phase(action: str, queued_at: float) -> contextlib.AbstractContextManager[None]:
    with contextlib.suppress(Exception):
        logger.info("presentation.%s.queue %.1fms", action, (monotonic() - queued_at) * 1000)
    return _phase(f"presentation.{action}.execute")


async def startup_stage[T](phase: str, work: Callable[[], Awaitable[T]]) -> T:
    with startup_phase(phase):
        return await work()


def startup_milestone(phase: str, started_at: float) -> None:
    """Report elapsed launch time; direct app users fall back to construction time."""
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
