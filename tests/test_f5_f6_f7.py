from __future__ import annotations

import asyncio

import pytest

from theater import proc
from theater.daemon import harness_detect
from theater.tmux import command


def test_pane_parse_rejects_a_non_numeric_pid():
    with pytest.raises(command.TmuxError, match="pane%1"):
        command.Pane.parse("pane%1\tnot-a-pid\t/tmp\t@1\tmain\twindow\tvibe")


@pytest.mark.parametrize("kill_fails", [False, True])
async def test_run_timeout_kills_and_waits(monkeypatch, kill_fails):
    events = []

    class Process:
        returncode = 0

        def __init__(self):
            self.communicate_calls = 0

        async def communicate(self):
            self.communicate_calls += 1
            await asyncio.Event().wait()

        def kill(self):
            events.append("kill")
            if kill_fails:
                raise ProcessLookupError

        async def wait(self):
            events.append("wait")

    process = Process()

    async def spawn(*args, **kwargs):
        return process

    monkeypatch.setattr(command, "_require", lambda: None)
    monkeypatch.setattr(command, "_run_timeout", lambda: 0.001)
    monkeypatch.setattr(command.asyncio, "create_subprocess_exec", spawn)

    with pytest.raises(command.TmuxError, match="tmux list-panes timed out"):
        await command.run("list-panes")

    assert events == ["kill", "wait"]
    assert process.communicate_calls == 1


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
