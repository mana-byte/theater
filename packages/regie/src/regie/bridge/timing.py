"""Bounded per-callback phase timing for the tmux provider bridge.

Mutations are always logged; frequent read callbacks only when slow, so the
periodic presence refresh cannot flood the bridge log.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping
from time import monotonic

from theater.frontend import CallbackHandler, CallbackRequest, CallbackResponse

logger = logging.getLogger("regie.bridge.latency")

#: Read callbacks run on every presence refresh; log them only past this bound.
SLOW_READ_MS = 100.0
READ_METHODS = frozenset({"terminal.inspect", "terminal.inventory"})


class CallbackTrace:
    def __init__(self, method: str, request: CallbackRequest) -> None:
        self.method = method
        self.operation_id = request.params.get("operation_id")
        self.terminal_id = request.params.get("terminal_id")
        self.started_at = monotonic()
        self.phases: list[tuple[str, float]] = []

    def emit(self, result: str) -> None:
        elapsed = (monotonic() - self.started_at) * 1000
        if self.method in READ_METHODS and elapsed < SLOW_READ_MS and result == "success":
            return
        phases = " ".join(f"{name}_ms={value:.1f}" for name, value in self.phases)
        context = "".join(
            f" {key}={value}"
            for key, value in (("operation", self.operation_id), ("terminal", self.terminal_id))
            if value is not None
        )
        logger.info(
            "callback.%s %.1fms%s result=%s%s",
            self.method,
            elapsed,
            context,
            result,
            f" {phases}" if phases else "",
        )


_current: contextvars.ContextVar[CallbackTrace | None] = contextvars.ContextVar(
    "regie_bridge_callback_trace", default=None
)


@contextlib.contextmanager
def phase(name: str) -> Iterator[None]:
    """Record one phase of the current callback; a no-op outside a traced callback."""
    trace = _current.get()
    started = monotonic()
    try:
        yield
    finally:
        if trace is not None:
            with contextlib.suppress(Exception):
                trace.phases.append((name, (monotonic() - started) * 1000))


def timed(method: str, handler: CallbackHandler) -> CallbackHandler:
    async def wrapper(request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        trace = CallbackTrace(method, request)
        token = _current.set(trace)
        result = "exception"
        try:
            response = await handler(request)
        except asyncio.CancelledError:
            result = "cancelled"
            raise
        else:
            result = _result(response)
            return response
        finally:
            _current.reset(token)
            with contextlib.suppress(Exception):
                trace.emit(result)

    return wrapper


def timed_handlers(
    handlers: Mapping[str, Callable[[CallbackRequest], Awaitable[object]]],
) -> dict[str, CallbackHandler]:
    return {method: timed(method, handler) for method, handler in handlers.items()}  # type: ignore[arg-type]


def _result(response: Mapping[str, object] | CallbackResponse) -> str:
    if isinstance(response, CallbackResponse):
        if response.error is not None:
            return str(response.error.get("code", "error"))
        return "success"
    delivery = response.get("delivery")
    if delivery is not None and delivery != "accepted":
        return str(delivery)
    return "success"


__all__ = ["SLOW_READ_MS", "phase", "timed", "timed_handlers"]
