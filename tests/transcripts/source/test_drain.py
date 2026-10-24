"""Chunked draining keeps cursors exact across rewrites, cancellation, rebinds and held seals."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
from pathlib import Path

import pytest

from tests.support.claude_seal import VERSION as SEAL_VERSION
from tests.support.shipped import ClaudeCodeObserver
from tests.support.transcript_source_rig import attach, record, source, transcript
from tests.support.transcript_source_rig import root as shared_root
from tests.support.transcript_source_rig import workdir as shared_workdir
from theater.harness.channels import filesystem
from theater.harness.source import Batch, TranscriptSource
from theater.harness.transcript import source as transcript_source

root = shared_root  # the shared fixture
workdir = shared_workdir  # the shared fixture


def _append(path: Path, lines: list[str]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(line + "\n")


async def test_a_large_drain_yields_to_other_loop_tasks(root, workdir, monkeypatch):
    """Large bursts drain in bounded batches with yields between them."""
    monkeypatch.setattr(transcript_source, "_DRAIN_READ_CHUNK_BYTES", 1 << 20)
    monkeypatch.setattr(transcript_source, "_DRAIN_PARSE_SLICE_RECORDS", 2)
    path = transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await attach(s)
    count = 400
    _append(path, [record(f"r{i}") for i in range(count)])

    beats = 0

    async def heartbeat():
        nonlocal beats
        while True:
            await asyncio.sleep(0)
            beats += 1

    heartbeat_task = asyncio.ensure_future(heartbeat())
    events = []
    batch_sizes = []
    try:
        while True:
            batch = await s.read()
            events.extend(batch.events)
            batch_sizes.append(len(batch.events))
            if not batch.has_more:
                break
            await asyncio.sleep(0)
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task

    assert beats >= count // 2 - 2
    assert max(batch_sizes) <= 2
    assert [e.text for e in events] == [f"r{i}" for i in range(count)]
    assert s.offset == path.stat().st_size
    assert s.index == 2 + count, "the attach point plus every drained record"


async def test_large_burst_bytes_are_read_once(root, workdir, monkeypatch):
    monkeypatch.setattr(transcript_source, "_DRAIN_READ_CHUNK_BYTES", 4096)
    monkeypatch.setattr(transcript_source, "_DRAIN_PARSE_SLICE_RECORDS", 2)
    path = transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await attach(s)
    lines = [record(f"r{i}") for i in range(1000)]
    payload_size = sum(len((line + "\n").encode()) for line in lines)
    _append(path, lines)

    real_open = Path.open
    bytes_read = 0

    class CountingFile:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            nonlocal bytes_read
            chunk = self.stream.read(size)
            bytes_read += len(chunk)
            return chunk

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def counted_open(candidate, *args, **kwargs):
        stream = real_open(candidate, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        return CountingFile(stream) if candidate == path and mode == "rb" else stream

    monkeypatch.setattr(Path, "open", counted_open)
    while (await s.read()).has_more:
        pass

    assert bytes_read == payload_size


async def test_chunked_draining_is_invisible_to_the_cursor(root, workdir, monkeypatch):
    """Chunk boundaries preserve events, offsets, and indexes."""
    appended = [
        record("short"),
        record("héllo-é-ü"),  # multi-byte characters straddle chunk edges
        record("x" * 200),  # one record far larger than the read chunk
        record("tail"),
    ]
    chunked_path = transcript(root, "aaa", workdir, record("old"))
    control_path = transcript(root, "bbb", workdir, record("old"))
    chunked = source(root, workdir, known_location=str(chunked_path))
    await attach(chunked)
    control = source(root, workdir, known_location=str(control_path))
    await attach(control)
    _append(chunked_path, appended)
    _append(control_path, appended)

    control_batch = await control.read()
    control_state = (control.offset, control.index)

    monkeypatch.setattr(transcript_source, "_DRAIN_READ_CHUNK_BYTES", 7)
    monkeypatch.setattr(transcript_source, "_DRAIN_PARSE_SLICE_RECORDS", 1)
    chunked_events = []
    while True:
        chunked_batch = await chunked.read()
        chunked_events.extend(chunked_batch.events)
        if not chunked_batch.has_more:
            break
    assert chunked.offset == control_path.stat().st_size, "the whole file was drained"
    chunked_state = (chunked.offset, chunked.index)

    assert [(e.text, e.source_offset) for e in chunked_events] == [
        (e.text, e.source_offset) for e in control_batch.events
    ]
    assert [e.text for e in chunked_events] == ["short", "héllo-é-ü", "x" * 200, "tail"]
    assert chunked_state == control_state
    assert chunked_batch.progressed == control_batch.progressed


async def test_a_partial_line_across_chunk_edges_is_never_lost(root, workdir, monkeypatch):
    """A record without its newline stays unread, then arrives exactly once."""
    monkeypatch.setattr(transcript_source, "_DRAIN_READ_CHUNK_BYTES", 7)
    monkeypatch.setattr(transcript_source, "_DRAIN_PARSE_SLICE_RECORDS", 1)
    path = transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await attach(s)
    cursor = (s.offset, s.index)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record("half"))

    partial = await s.read()
    assert partial.events == ()
    assert (s.offset, s.index) == cursor, "a partial record must not move the cursor"

    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    _append(path, [record("whole")])
    events = []
    while True:
        batch = await s.read()
        events.extend(batch.events)
        if not batch.has_more:
            break
    assert [e.text for e in events] == ["half", "whole"]
    assert s.offset == path.stat().st_size
    assert s.index == cursor[1] + 2


async def test_a_rewrite_detected_mid_drain_rereads_from_the_top(root, workdir, monkeypatch):
    """Truncation resets the cursor to zero and re-emits the new content."""
    monkeypatch.setattr(transcript_source, "_DRAIN_READ_CHUNK_BYTES", 32)
    monkeypatch.setattr(transcript_source, "_DRAIN_PARSE_SLICE_RECORDS", 2)
    path = transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await attach(s)
    _append(path, [record(f"r{i}") for i in range(50)])
    while (await s.read()).has_more:
        pass

    path.write_text(
        "\n".join([json.dumps({"type": "system", "cwd": workdir}), record("fresh-a")]) + "\n",
        encoding="utf-8",
    )
    events = []
    progressed = False
    while True:
        batch = await s.read()
        events.extend(batch.events)
        progressed |= batch.progressed
        if not batch.has_more:
            break
    assert [e.text for e in events] == ["fresh-a"]
    assert progressed is True
    assert s.offset == path.stat().st_size
    assert s.index == 2, "the whole rewritten file counts again"


async def test_a_cancelled_drain_replays_without_skips_or_duplicates(root, workdir, monkeypatch):
    """Cancellation before a complete record leaves parser state untouched."""
    monkeypatch.setattr(transcript_source, "_DRAIN_READ_CHUNK_BYTES", 8)
    monkeypatch.setattr(transcript_source, "_DRAIN_PARSE_SLICE_RECORDS", 2)
    path = transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await attach(s)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record("x" * 2_000))

    parse_calls = 0
    original_parse = s._parse_record

    def counting_parse(line, index, *, clip_text):
        nonlocal parse_calls
        parse_calls += 1
        return original_parse(line, index, clip_text=clip_text)

    monkeypatch.setattr(s, "_parse_record", counting_parse)
    cursor = (s.offset, s.index)
    task = asyncio.ensure_future(s.read())
    while not s._draining and not task.done():
        await asyncio.sleep(0)
    assert not task.done()
    await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert parse_calls == 0
    assert (s.offset, s.index) == cursor, "a cancelled drain must not move the cursor"
    assert s._draining is False, "the re-entrancy flag is cleared by cancellation"

    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n")
    batch = await s.read()
    assert [e.text for e in batch.events] == ["x" * 2_000]
    assert parse_calls == 1
    assert s.offset == path.stat().st_size


async def test_a_read_during_a_suspended_drain_is_refused(root, workdir, monkeypatch):
    """A second read cannot race a suspended oversized-record scan."""
    monkeypatch.setattr(transcript_source, "_DRAIN_READ_CHUNK_BYTES", 8)
    monkeypatch.setattr(transcript_source, "_DRAIN_PARSE_SLICE_RECORDS", 2)
    path = transcript(root, "aaa", workdir, record("old"))
    s = source(root, workdir)
    await attach(s)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(record("x" * 2_000))

    task = asyncio.ensure_future(s.read())
    while not s._draining and not task.done():
        await asyncio.sleep(0)
    assert not task.done(), "the drain must still be suspended mid-read"
    with pytest.raises(RuntimeError, match="already in progress"):
        await s.read()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert s._draining is False


async def test_exact_rebind_during_drain_discards_old_batch(root, workdir, monkeypatch):
    monkeypatch.setattr(transcript_source, "_DRAIN_READ_CHUNK_BYTES", 8)
    old = transcript(root, "aaa", workdir, record("old"))
    replacement = transcript(root, "bbb", workdir, record("replacement"))
    s = source(root, workdir, known_location=str(old))
    await attach(s)
    with old.open("a", encoding="utf-8") as fh:
        fh.write(record("stale"))

    task = asyncio.create_task(s.read())
    while not s._drain_buffer and not task.done():
        await asyncio.sleep(0)
    assert not task.done()
    assert s.admit_exact_location(location=str(replacement), session_id="bbb") == "staged"

    stale = await task

    assert stale.events == ()
    assert stale.waiting is True
    assert s.path is None
    attached = await s.read()
    assert attached.attached is not None
    assert attached.attached.location == str(replacement)


async def test_exact_rebind_during_attachment_discards_old_candidate(root, workdir, monkeypatch):
    old = transcript(root, "aaa", workdir, record("old"))
    replacement = transcript(root, "bbb", workdir, record("replacement"))
    s = source(root, workdir, known_location=str(old))
    started = threading.Event()
    release = threading.Event()
    real_attach_point = transcript_source.attach_point

    def blocked_attach_point(path, *args):
        if path == old:
            started.set()
            assert release.wait(timeout=5)
        return real_attach_point(path, *args)

    monkeypatch.setattr(transcript_source, "attach_point", blocked_attach_point)
    task = asyncio.create_task(s.read())
    while not started.is_set():
        await asyncio.sleep(0)
    assert s.admit_exact_location(location=str(replacement), session_id="bbb") == "staged"
    release.set()

    stale = await task

    assert stale == Batch(waiting=True)
    assert s.path is None
    assert s._pending is None
    attached = await s.read()
    assert attached.attached is not None
    assert attached.attached.location == str(replacement)


def _cost_state(duration: int) -> str:
    return json.dumps({"type": "cost-state", "sessionId": "s", "totalDuration": duration})


def _replace_last_line(path: Path, line: str) -> None:
    data = path.read_bytes()
    start = data.rstrip(b"\n").rfind(b"\n") + 1
    path.write_bytes(data[:start] + line.encode() + b"\n")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))


async def _drain_texts(s) -> list[str]:
    texts = []
    while True:
        batch = await s.read()
        texts.extend(e.text for e in batch.events)
        if not batch.has_more:
            return texts


async def test_a_cost_state_rewritten_in_place_replays_nothing(root, workdir, monkeypatch):
    """Claude rewrites its trailing cost-state hourly; re-reading the file would duplicate it."""
    monkeypatch.setattr(filesystem, "kqueue_available", lambda: False)
    path = transcript(root, "aaa", workdir)
    s = source(root, workdir)
    await attach(s)
    _append(path, [record("a"), record("b"), _cost_state(10_000)])
    assert await _drain_texts(s) == ["a", "b"]
    held = (s.offset, s.index)

    _replace_last_line(path, _cost_state(20_000))  # same length, new mtime
    assert await _drain_texts(s) == []
    assert (s.offset, s.index) == held
    _replace_last_line(path, _cost_state(3_000_000))  # longer
    assert await _drain_texts(s) == []
    _replace_last_line(path, record("c"))
    _append(path, [_cost_state(3_000_001)])
    assert await _drain_texts(s) == ["c"]


async def test_a_cost_state_followed_by_records_is_consumed(root, workdir, monkeypatch):
    monkeypatch.setattr(filesystem, "kqueue_available", lambda: False)
    path = transcript(root, "aaa", workdir, record("old"), _cost_state(1))
    s = source(root, workdir)
    await attach(s)
    _append(path, [record("resumed")])
    assert await _drain_texts(s) == ["resumed"]
    assert s.offset == path.stat().st_size


def _held_marker_lines() -> list[str]:
    """An out-of-order turn: a1's parentUuid ("late") is an attachment flushed after the marker."""

    def rec(**kw) -> str:
        return json.dumps({"version": SEAL_VERSION, **kw})

    return [
        rec(
            type="assistant",
            uuid="a1",
            parentUuid="late",
            message={
                "id": "m1",
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done"}],
            },
        ),
        rec(type="attachment", uuid="att1", parentUuid="a1"),
        rec(type="system", subtype="turn_duration", uuid="mk", parentUuid="att1"),
    ]


async def _hold_marker(root, workdir, name):
    path = transcript(root, name, workdir)
    s = source(root, workdir)
    await attach(s)
    _append(path, _held_marker_lines())
    await s.read()
    assert s._observer._seal._deferred is not None  # held: parent "late" not read yet
    return s, path


async def test_a_stream_discontinuity_resets_the_held_claude_seal(root, workdir):
    # Detach (the file vanished) drops the held marker.
    s, path = await _hold_marker(root, workdir, "aaa")
    path.unlink()
    await s.read()
    assert s._observer._seal._deferred is None

    # Rewrite (the file shrank) drops the held marker.
    s, path = await _hold_marker(root, workdir, "bbb")
    path.write_text(json.dumps({"type": "system", "cwd": workdir}) + "\n", encoding="utf-8")
    await s.read()
    assert s._observer._seal._deferred is None

    # (Re-)attaching to a file with content resets before its last line is parsed.
    path = transcript(root, "ccc", workdir, _held_marker_lines()[0])
    s = source(root, workdir)
    calls: list[int] = []
    real = s._observer.reset_stream
    s._observer.reset_stream = lambda: calls.append(1) or real()
    await s.read()
    assert calls, "the attach path must call the stream-reset hook"

    # Exact relocation onto an EMPTY file (last_line is None, no detach) still resets the seal.
    observer = ClaudeCodeObserver(root=root)
    for i, line in enumerate(_held_marker_lines()):
        observer.parse(line, i)
    assert observer._seal._deferred is not None
    empty = root / "-work" / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    await TranscriptSource(observer, cwd=workdir, allow_refresh=True)._attach(path=empty)
    assert observer._seal._deferred is None

    # An observer without the optional hook is untouched (no-op, no error).
    bare = source(root, workdir)
    bare._observer = object()
    bare._reset_stream()
