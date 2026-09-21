"""Action-phase measurements independent of UI state and process logging setup."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from regie.controllers.actions import ActionRecord

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def action_phase(record: ActionRecord, phase: str) -> Iterator[None]:
    """Measure one phase without changing its result, exception, or cancellation."""
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
                    "action.%s.%s %.1fms operation=%s result=%s",
                    record.action,
                    phase,
                    (monotonic() - started) * 1000,
                    record.operation_id,
                    result,
                )
