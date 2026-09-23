"""Per-callback phase timing for the tmux provider bridge.

Mutations are always logged; frequent reads only when slow or refused.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
from asyncio import CancelledError
from collections.abc import Iterator, Mapping
from time import monotonic

from theater.frontend import CallbackHandler, CallbackRequest, CallbackResponse

logger = logging.getLogger("regie.bridge.latency")
SLOW_READ_MS = 100.0
_READS = frozenset({"terminal.inspect", "terminal.inventory"})
_phases: contextvars.ContextVar[list[tuple[str, float]] | None] = contextvars.ContextVar(
    "regie_bridge_phases", default=None
)


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    """Record a phase of the current callback; a no-op outside one."""
    started = monotonic()
    try:
        yield
    finally:
        if (phases := _phases.get()) is not None:
            phases.append((name, (monotonic() - started) * 1000))


def timed_handlers(handlers: Mapping[str, CallbackHandler]) -> dict[str, CallbackHandler]:
    return {method: _timed(method, handler) for method, handler in handlers.items()}


def _timed(method: str, handler: CallbackHandler) -> CallbackHandler:
    async def wrapper(request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        started, phases, result = monotonic(), [], "exception"
        token = _phases.set(phases)
        try:
            response = await handler(request)
        except CancelledError:
            result = "cancelled"
            raise
        else:
            result = _outcome(response)
            return response
        finally:
            _phases.reset(token)
            with contextlib.suppress(Exception):
                _emit(method, request, (monotonic() - started) * 1000, result, phases)

    return wrapper


def _emit(method: str, request: CallbackRequest, elapsed: float, result: str, phases: list) -> None:
    if method in _READS and result == "success" and elapsed < SLOW_READ_MS:
        return
    fields = [
        f"{key}={request.params[key]}"
        for key in ("operation_id", "terminal_id")
        if request.params.get(key) is not None
    ]
    fields.append(f"result={result}")
    fields += [f"{name}_ms={value:.1f}" for name, value in phases]
    logger.info("callback.%s %.1fms %s", method, elapsed, " ".join(fields))


def _outcome(response: Mapping[str, object] | CallbackResponse) -> str:
    if isinstance(response, CallbackResponse):
        return "success" if response.error is None else str(response.error.get("code", "error"))
    delivery = response.get("delivery", "accepted")
    return "success" if delivery == "accepted" else str(delivery)


__all__ = ["SLOW_READ_MS", "phase", "timed_handlers"]
