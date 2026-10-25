"""A slow terminal names itself: blocked writes and a writer that has fallen behind."""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest
from regie import terminal_timing
from regie.terminal_timing import TimedFile, TimedLinuxDriver, timed
from textual.drivers.headless_driver import HeadlessDriver
from textual.drivers.linux_driver import LinuxDriver


class _Pty:
    """A terminal that takes a given time to accept each write."""

    encoding = "utf-8"

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.written: list[str] = []
        self.flushed = 0

    def write(self, data: str) -> int:
        time.sleep(self.delay)
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        time.sleep(self.delay)
        self.flushed += 1

    def fileno(self) -> int:
        return 7


@pytest.fixture(autouse=True)
def _capture(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="regie.latency")


def test_a_write_that_blocks_is_logged_with_its_size_and_a_quick_one_is_not(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(terminal_timing, "_SLOW_WRITE_SECONDS", 0.02)
    quick = TimedFile(_Pty())
    quick.write("fast")
    quick.flush()
    assert not caplog.messages

    slow_pty = _Pty(delay=0.05)
    slow = TimedFile(slow_pty)
    assert slow.write("frame") == 5
    slow.flush()

    assert slow_pty.written == ["frame"] and slow_pty.flushed == 1
    first, second = caplog.messages
    assert first.startswith("terminal.write blocked") and first.endswith("(5 chars)")
    assert second.startswith("terminal.flush blocked")
    assert all(r.levelno == logging.WARNING for r in caplog.records)


def test_the_wrapper_still_looks_like_the_file_it_wraps() -> None:
    wrapped = TimedFile(_Pty())
    assert wrapped.fileno() == 7 and wrapped.encoding == "utf-8"


def test_only_the_posix_driver_is_replaced() -> None:
    assert timed(LinuxDriver) is TimedLinuxDriver and issubclass(TimedLinuxDriver, LinuxDriver)
    assert timed(HeadlessDriver) is HeadlessDriver


def test_a_writer_with_frames_piling_up_is_reported_at_most_once_a_second(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _Queue:
        def __init__(self, depth: int) -> None:
            self.depth = depth

        def qsize(self) -> int:
            return self.depth

    class _Writer:
        def __init__(self) -> None:
            self._queue = _Queue(2)

        def write(self, _data: str) -> None:
            return None

    driver: Any = object.__new__(TimedLinuxDriver)
    driver._writer_thread = _Writer()
    driver._queue_reported_at = 0.0

    driver.write("a")  # shallow: nothing to say
    assert not caplog.messages

    driver._writer_thread._queue.depth = terminal_timing._QUEUE_WARN_DEPTH + 2
    driver.write("b")
    driver.write("c")  # the same second
    assert caplog.messages == [
        f"terminal.queue {terminal_timing._QUEUE_WARN_DEPTH + 2} frames waiting for the writer"
    ]


def test_the_app_asks_for_the_timed_driver() -> None:
    from regie.app import RegieApp

    assert issubclass(RegieApp.get_driver_class(object.__new__(RegieApp)), LinuxDriver)
