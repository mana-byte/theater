"""Race-safe observer wakeups with a polling fallback.

A live channel delivers data asynchronously, and the observation watch loop
must notice it promptly without spawning a task per message. The signal below
is the whole mechanism: a producer calls :meth:`WakeupSignal.wake` when live
data has arrived, and the consumer sleeps on the same signal with its ordinary
poll interval as the timeout. Two properties make it race-safe:

* the consumer clears the signal *before* reading the source, so data that
  arrives while a read is already in flight sets the signal again and the
  following sleep returns immediately — a wake is never lost to a read;
* the timeout is the polling fallback, so a producer that never calls
  :meth:`WakeupSignal.wake` (or a wake that races a stopping daemon) degrades
  to exactly the polling behaviour that existed before live wiring.

This module is a generic harness-side helper: it imports nothing from
``theater.daemon`` and knows nothing about participants. The per-participant
mapping lives in the daemon observation live hub.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable


class WakeupSignal:
    """One race-safe wake event with a timeout-driven polling fallback."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def wake(self) -> None:
        """Announce that new live data is available for reading."""
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def consume(self) -> bool:
        """Clear the signal and report whether it was set.

        The consumer calls this immediately before reading its source: any
        wake that arrives during the read re-sets the signal, so the next
        sleep ends promptly and the data is read again.
        """
        was_set = self._event.is_set()
        self._event.clear()
        return was_set

    async def wait(self) -> None:
        await self._event.wait()

    async def sleep_until(
        self,
        stop: asyncio.Event,
        *,
        timeout: float,
    ) -> None:
        """Sleep until woken, stopped, or the timeout elapses.

        The timeout is the polling fallback — never a busy loop, and never an
        unbounded wait. Both waiters are cleaned up on every exit path so a
        cancelled observer cannot leak tasks.
        """
        tasks = {
            asyncio.ensure_future(stop.wait()),
            asyncio.ensure_future(self._event.wait()),
        }
        try:
            await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


class WakeupHub:
    """Bounded per-key wake signals for one owner.

    Keys are opaque (participant ids in the daemon); one signal exists per
    registered key and unregistered keys are discarded, so the hub's size is
    bounded by its owner's registrations — never by message traffic.
    """

    def __init__(self) -> None:
        self._signals: dict[str, WakeupSignal] = {}

    def signal(self, key: str) -> WakeupSignal:
        """The signal for one key, created on first use."""
        if not isinstance(key, str) or not key:
            raise ValueError("wakeup key must be a non-blank string")
        signal = self._signals.get(key)
        if signal is None:
            signal = WakeupSignal()
            self._signals[key] = signal
        return signal

    def existing(self, key: str) -> WakeupSignal | None:
        """The signal for one key, or None when the key was never registered."""
        return self._signals.get(key)

    def wake(self, key: str) -> None:
        """Wake one key; a key with no signal yet wakes on first registration."""
        self.signal(key).wake()

    def wake_all(self, keys: Iterable[str]) -> None:
        for key in keys:
            self.wake(key)

    def discard(self, key: str) -> None:
        self._signals.pop(key, None)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._signals)

    def clear(self) -> None:
        self._signals.clear()


__all__ = ["WakeupHub", "WakeupSignal"]
