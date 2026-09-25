"""Race-safe observer wakeups with a polling fallback.

The consumer clears before reading, so a wake during a read is never lost; the poll timeout
keeps pre-live behaviour when nobody wakes. Imports nothing from ``theater.daemon``.
"""

from __future__ import annotations

import asyncio


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
        """Clear the signal and report whether it was set; call immediately before reading."""
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

        Both waiters are cleaned up on every exit so a cancelled observer cannot leak tasks.
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

    Size is bounded by registrations, never by message traffic.
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

    def discard(self, key: str) -> None:
        self._signals.pop(key, None)

    def keys(self) -> tuple[str, ...]:
        return tuple(self._signals)

    def clear(self) -> None:
        self._signals.clear()


__all__ = ["WakeupHub", "WakeupSignal"]
