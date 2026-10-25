"""Name a slow terminal: time the writes Textual makes to it, and how many frames are waiting.

Textual composes a frame on the event loop, then hands it to a writer thread through a queue
of thirty. Anything slow after that point (a pty tmux has stopped draining, a client that
reads late) delays what the human sees while every timer inside Régie still runs on time.
That is invisible to the loop-lag monitor, so this logs it: a write that blocked, and a queue
that is filling up.
"""

from __future__ import annotations

import contextlib
import logging
from time import monotonic
from typing import Any

from textual.drivers.linux_driver import LinuxDriver

logger = logging.getLogger("regie.latency")

#: A write to the terminal that blocks this long is logged.
_SLOW_WRITE_SECONDS = 0.1

#: This many frames waiting for the writer is logged, at most once a second.
_QUEUE_WARN_DEPTH = 8
_QUEUE_WARN_EVERY_SECONDS = 1.0


class TimedFile:
    """The terminal's file, with every blocking write or flush that took too long reported."""

    def __init__(self, file: Any) -> None:
        self._file = file

    def write(self, data: str) -> int:
        started = monotonic()
        written = self._file.write(data)
        self._report("write", started, len(data))
        return written

    def flush(self) -> None:
        started = monotonic()
        self._file.flush()
        self._report("flush", started, 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._file, name)

    @staticmethod
    def _report(what: str, started: float, size: int) -> None:
        elapsed = monotonic() - started
        if elapsed >= _SLOW_WRITE_SECONDS:
            logger.warning("terminal.%s blocked %.0fms (%d chars)", what, elapsed * 1000, size)


class TimedLinuxDriver(LinuxDriver):
    """Textual's POSIX driver, reporting a terminal that cannot keep up."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._file = TimedFile(self._file)  # type: ignore[assignment]
        self._queue_reported_at = 0.0

    def write(self, data: str) -> None:
        super().write(data)
        with contextlib.suppress(Exception):
            depth = self._writer_thread._queue.qsize()  # type: ignore[union-attr]
            now = monotonic()
            if (
                depth >= _QUEUE_WARN_DEPTH
                and now - self._queue_reported_at >= _QUEUE_WARN_EVERY_SECONDS
            ):
                self._queue_reported_at = now
                logger.warning("terminal.queue %d frames waiting for the writer", depth)


def timed(driver: type) -> type:
    """*driver*, or its timed equivalent where there is one."""
    return TimedLinuxDriver if driver is LinuxDriver else driver


__all__ = ["TimedFile", "TimedLinuxDriver", "timed"]
