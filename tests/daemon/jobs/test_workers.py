"""Admission capacity belongs to execution, not a cancellable waiter."""

import asyncio
import threading

import pytest

from theater.daemon import workers


async def test_worker_admission_is_bounded_and_cancellation_does_not_release_running_work(
    monkeypatch, caplog
):
    await workers.shutdown()
    monkeypatch.setattr(workers, "MAX_WORKERS", 1)
    caplog.set_level("DEBUG", logger="theater.timing")
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    ran: list[str] = []

    def blocking():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)

    first = asyncio.create_task(workers.to_thread(blocking, label="blocked"))
    try:
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        queued = asyncio.create_task(workers.to_thread(ran.append, "cancelled", label="queued"))
        await asyncio.sleep(0)
        assert not queued.done()
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        remaining = asyncio.create_task(workers.to_thread(ran.append, "done", label="remaining"))
        await asyncio.sleep(0)
        assert not remaining.done()
        release.set()
        await remaining
    finally:
        release.set()
        await workers.shutdown()
    assert ran == ["done"]
    operations = {getattr(record, "theater.operation", None) for record in caplog.records}
    assert {"WORKER_WAIT", "WORKER_EXECUTION"} <= operations
