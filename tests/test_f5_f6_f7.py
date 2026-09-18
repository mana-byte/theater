from __future__ import annotations

from theater import proc
from theater.daemon import harness_detect


async def test_detect_harness_async_fast_path_does_not_capture(monkeypatch):
    def fail_capture():
        raise AssertionError("fast path must not capture a process snapshot")

    monkeypatch.setattr(proc.ProcessSnapshot, "capture", staticmethod(fail_capture))
    result = await harness_detect.detect_harness_async(
        "vibe", 123, detector=harness_detect.detect_harness
    )
    assert result == "vibe"


async def test_detect_harness_async_captures_once_and_uses_snapshot(monkeypatch):
    snapshot = proc.ProcessSnapshot(_children={123: [(456, "vibe")]})
    capture_calls = 0
    to_thread_calls = []

    def capture():
        nonlocal capture_calls
        capture_calls += 1
        return snapshot

    async def to_thread(fn, /, *args, label):
        to_thread_calls.append((fn, label))
        return fn(*args)

    monkeypatch.setattr(proc.ProcessSnapshot, "capture", staticmethod(capture))
    monkeypatch.setattr(harness_detect.workers, "to_thread", to_thread)

    result = await harness_detect.detect_harness_async(
        "python3", 123, detector=harness_detect.detect_harness
    )

    assert capture_calls == 1
    # The label is not cosmetic: workers forward it into observability, where it
    # names the log, trace, and metric task dimension — pin it.
    assert to_thread_calls == [(proc.ProcessSnapshot.capture, "harness_detect")]
    assert result == harness_detect.detect_harness("python3", 123, snapshot=snapshot)
