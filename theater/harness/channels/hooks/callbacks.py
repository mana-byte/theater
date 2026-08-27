"""Bounded off-loop hook callback execution."""

from __future__ import annotations

import asyncio
import contextvars
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

from theater.constants.harness import (
    HARNESS_HOOK_CALLBACK_MAX_IN_FLIGHT,
    HARNESS_HOOK_CORRELATION_TIMEOUT_SECONDS,
    HARNESS_HOOK_DECODER_TIMEOUT_SECONDS,
)
from theater.harness.contracts.callbacks import HookCorrelationContext, HookDecodeContext

_Context = TypeVar("_Context")
_Result = TypeVar("_Result")


class HookCallbackBusy(RuntimeError):
    """All bounded hook callback capacity is occupied."""


class HookCallbackTimeout(RuntimeError):
    """One hook callback exceeded its bounded execution time."""


class HookCallbackRunner:
    """Own bounded worker execution for one hook runtime."""

    def __init__(
        self,
        *,
        max_in_flight: int = HARNESS_HOOK_CALLBACK_MAX_IN_FLIGHT,
        correlation_timeout: float = HARNESS_HOOK_CORRELATION_TIMEOUT_SECONDS,
        decoder_timeout: float = HARNESS_HOOK_DECODER_TIMEOUT_SECONDS,
    ) -> None:
        if type(max_in_flight) is not int or max_in_flight <= 0:
            raise ValueError("hook callback capacity must be a positive integer")
        if not math.isfinite(correlation_timeout) or correlation_timeout <= 0:
            raise ValueError("hook correlation timeout must be finite and positive")
        if not math.isfinite(decoder_timeout) or decoder_timeout <= 0:
            raise ValueError("hook decoder timeout must be finite and positive")
        self._executor = ThreadPoolExecutor(
            max_workers=max_in_flight, thread_name_prefix="theater-hook"
        )
        self._max_in_flight = max_in_flight
        self._correlation_timeout = correlation_timeout
        self._decoder_timeout = decoder_timeout
        self._in_flight = 0
        self._closed = False

    async def correlate(
        self,
        callback: Callable[[HookCorrelationContext], str],
        context: HookCorrelationContext,
    ) -> str:
        return await self._run(callback, context, timeout=self._correlation_timeout)

    async def decode(
        self,
        callback: Callable[[HookDecodeContext], _Result],
        context: HookDecodeContext,
    ) -> _Result:
        return await self._run(callback, context, timeout=self._decoder_timeout)

    async def _run(
        self,
        callback: Callable[[_Context], _Result],
        context: _Context,
        *,
        timeout: float,
    ) -> _Result:
        if self._closed or self._in_flight >= self._max_in_flight:
            raise HookCallbackBusy
        self._in_flight += 1
        current = contextvars.copy_context()
        future = asyncio.get_running_loop().run_in_executor(
            self._executor, current.run, callback, context
        )
        future.add_done_callback(self._finished)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except TimeoutError:
            raise HookCallbackTimeout from None

    def _finished(self, future: asyncio.Future) -> None:
        if not future.cancelled():
            future.exception()
        self._in_flight -= 1

    async def aclose(self) -> None:
        self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


__all__ = ["HookCallbackBusy", "HookCallbackRunner", "HookCallbackTimeout"]
