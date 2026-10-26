"""The attach scan is one forward pass: counts, identity and a bounded, parser-exact tail."""

from __future__ import annotations

import asyncio
import tracemalloc
from pathlib import Path

import pytest

from tests.support.shipped import ClaudeCodeObserver
from tests.support.transcript_source_rig import record, transcript
from tests.support.transcript_source_rig import root as shared_root
from tests.support.transcript_source_rig import workdir as shared_workdir
from theater.harness.source import Attachment, AttachSnapshot, TranscriptSource
from theater.harness.transcript import attachment as attachment_module
from theater.harness.transcript.attachment import TailRecord, attach_point

root = shared_root  # the shared fixture
workdir = shared_workdir  # the shared fixture

# Multibyte characters of 2 and 4 bytes, so small chunk sizes split them mid-character.
LINES = [f"{index}:" + "é😀" * index for index in range(20)]


def _expected_tail(data: bytes, tail: int) -> tuple[TailRecord, ...]:
    """The forward parser's view: split on newlines, accumulating offsets from the top."""
    records, end = [], 0
    for raw_index, raw in enumerate(data.split(b"\n")[:-1]):
        end += len(raw) + 1
        records.append(TailRecord(raw_index, end, raw.decode("utf-8", "replace")))
    return tuple(records[-tail:]) if tail else ()


def _legacy_last_line(data: bytes) -> str | None:
    """The pre-snapshot ``attach_point`` algorithm, which held the whole file."""
    if data.count(b"\n") == 0:
        return None
    head, _sep, _rest = data.rpartition(b"\n")
    return head.rpartition(b"\n")[2].decode("utf-8", errors="replace")


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


@pytest.mark.parametrize("chunk_bytes", [1, 7, 64, 4096])
@pytest.mark.parametrize("tail", [0, 1, 3, 19, 20, 50])
def test_tail_lines_up_with_the_forward_parser(tmp_path, monkeypatch, chunk_bytes, tail):
    data = "".join(line + "\n" for line in LINES).encode()
    path = _write(tmp_path / "records.jsonl", data)
    monkeypatch.setattr(attachment_module, "HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES", chunk_bytes)

    snapshot = attach_point(path, tail)

    st = path.stat()
    assert (snapshot.size, snapshot.lines, snapshot.trailing_bytes) == (len(data), 20, 0)
    assert (snapshot.mtime, snapshot.dev, snapshot.ino) == (st.st_mtime_ns, st.st_dev, st.st_ino)
    assert snapshot.tail == _expected_tail(data, tail)
    assert len(snapshot.tail) == min(tail, 20)


@pytest.mark.parametrize("chunk_bytes", [1, 5, 4096])
@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"only partial",
        b"\n",
        b"one\n",
        b"one\ntwo\nthree",
        b"one\n\n\nlast\n",
        "é😀\n😀é\n".encode(),
        "é😀\n😀".encode()[:-1],
    ],
    ids=[
        "empty",
        "no_newline",
        "blank_record",
        "single",
        "partial_trailing",
        "blank_records",
        "multibyte",
        "cut_multibyte_trailing",
    ],
)
def test_shapes_keep_the_old_last_line_and_the_forward_view(
    tmp_path, monkeypatch, chunk_bytes, data
):
    monkeypatch.setattr(attachment_module, "HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES", chunk_bytes)
    snapshot = attach_point(_write(tmp_path / "records.jsonl", data))

    assert snapshot.last_line == _legacy_last_line(data)
    assert snapshot.tail == _expected_tail(data, 1)
    assert snapshot.size == len(data)
    assert snapshot.lines == data.count(b"\n")
    assert snapshot.trailing_bytes == len(data) - (data.rfind(b"\n") + 1)


def test_scan_of_a_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        attach_point(tmp_path / "missing.jsonl")


def test_a_large_file_is_not_held_in_memory(tmp_path, monkeypatch):
    chunk_bytes = 1 << 16
    monkeypatch.setattr(attachment_module, "HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES", chunk_bytes)
    line = b"x" * 99 + b"\n"
    path = _write(tmp_path / "big.jsonl", line * (80 * chunk_bytes // len(line)))
    assert path.stat().st_size > 32 * chunk_bytes

    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        snapshot = attach_point(path, tail=5)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert [record.raw_index for record in snapshot.tail] == list(
        range(snapshot.lines - 5, snapshot.lines)
    )
    # The last 5 records, a straddling chunk, the read in flight and the join: never the file.
    assert peak < 6 * chunk_bytes


class _Probe(TranscriptSource):
    """Records what the staging hook was given and lets a test steer it."""

    _attach_tail = 3

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.hook = None
        self.seen: list[AttachSnapshot] = []

    async def _stage_attachment(self, snapshot: AttachSnapshot, attachment: Attachment):
        self.seen.append(snapshot)
        if self.hook is None:
            return await super()._stage_attachment(snapshot, attachment)
        return await self.hook(self, snapshot, attachment)


def _probe(root, workdir) -> tuple[_Probe, ClaudeCodeObserver]:
    observer = ClaudeCodeObserver(root=root)
    return _Probe(observer, cwd=workdir, allow_refresh=True), observer


async def test_attach_hands_the_class_tail_to_the_staging_hook(root, workdir):
    path = transcript(root, "aaa", workdir, *[record(f"r{index}") for index in range(6)])
    s, _ = _probe(root, workdir)

    batch = await s.read()

    snapshot = s.seen[0]
    assert [r.raw_index for r in snapshot.tail] == [4, 5, 6]
    assert snapshot.lines == 7
    assert snapshot.tail[-1].end == snapshot.size == path.stat().st_size
    assert batch.attached is not None and batch.attached.skipped == 7


async def test_default_hook_keeps_the_staged_attachment(root, workdir):
    transcript(root, "aaa", workdir, record("one"))
    s, _ = _probe(root, workdir)

    staged = await s.read()
    s.commit_attachment()

    assert staged.attached.last_event.raw_text == "one"
    assert (s.offset, s.index) == (s.seen[0].size, s.seen[0].lines)


async def test_restage_moves_the_read_position_the_live_read_replays_from(root, workdir):
    transcript(root, "aaa", workdir, *[record(f"r{index}") for index in range(4)])
    s, _ = _probe(root, workdir)

    async def replay_last_two(source, snapshot, attachment):
        source._restage(offset=snapshot.tail[-3].end, index=snapshot.tail[-2].raw_index)
        return attachment

    s.hook = replay_last_two
    await s.read()
    s.commit_attachment()

    batch = await s.read()
    assert [event.raw_text for event in batch.events] == ["r2", "r3"]
    assert s.index == s.seen[0].lines


@pytest.mark.parametrize("failure", [OSError("gone"), asyncio.CancelledError(), ValueError("bug")])
async def test_a_failed_hook_leaves_no_candidate_and_no_primed_stream(
    root, workdir, monkeypatch, failure
):
    transcript(root, "aaa", workdir, record("one"))
    s, observer = _probe(root, workdir)
    resets = []
    monkeypatch.setattr(observer, "reset_stream", lambda: resets.append(s._pending))

    async def fail(source, snapshot, attachment):
        resets.clear()
        raise failure

    s.hook = fail
    with pytest.raises(type(failure)):
        await s._attach()

    assert s._pending is None
    assert resets == [None]  # the stream restarted only after the candidate was dropped

    s.hook = None
    assert (await s.read()).attached is not None
