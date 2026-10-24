"""Closing an OpenCode source waits for cancelled SQLite workers and never reopens it."""

import asyncio
import threading

import pytest

from tests.support.opencode_recorder_v2 import rec as shared_rec
from theater.harness.builtin.plugins.opencode.source_v2 import OpenCodeV2Source

rec = shared_rec


async def test_close_waits_for_a_cancelled_read_worker(rec, workdir, monkeypatch):
    source = OpenCodeV2Source(rec.path, cwd=str(workdir), session_id=rec.sid)
    assert (await source.read()).attached is not None
    source.commit_attachment()
    connection = source._conn
    started, release = threading.Event(), threading.Event()
    drain = source._drain

    def held(conn):
        started.set()
        assert release.wait(10), "the test never released its worker"
        return drain(conn)

    monkeypatch.setattr(source, "_drain", held)
    reading = asyncio.create_task(source.read())
    assert await asyncio.to_thread(started.wait, 5)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    closing_started = asyncio.Event()

    async def close():
        closing_started.set()
        await source.aclose()

    closing = asyncio.create_task(close())
    await closing_started.wait()
    try:
        assert not closing.done()
        assert source._conn is connection, "SQLite closed while its worker was still running"
    finally:
        release.set()
        await closing
    assert source._conn is None
    assert (await source.read()).error_code == "transcript_source_unavailable"
    assert source._conn is None
