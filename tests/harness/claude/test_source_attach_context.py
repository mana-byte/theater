"""Claude attachment primes a bounded, unchanged causal prefix without replaying history."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.support.claude_seal import claude_fragment, claude_marker, claude_record
from tests.support.observation import apply_batch
from theater.constants.harness import HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES
from theater.daemon.jobs import JobManager
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness.builtin.plugins.claude import source as claude_source
from theater.harness.builtin.plugins.claude.constants import SEAL_NODE_LIMIT
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.source import StreamPoint
from theater.models import JobState
from theater.provenance import TranscriptProvenance
from theater.resume_floor import UNKNOWN_FLOOR, encode_floor
from theater.transcript_identity import TRANSCRIPT_SOURCE_UNAVAILABLE_CODE

SESSION = "11111111-1111-4111-8111-111111111111"
INITIAL = (
    claude_record("user", None, "user", message={"role": "user", "content": "go"}),
    claude_record("context", "user", "attachment", attachment={"type": "prompt_snapshot"}),
)
ANSWER = (
    claude_fragment("answer", "context", {"type": "text", "text": "DONE"}),
    claude_record("snapshot", "answer", "attachment", attachment={"type": "prompt_snapshot"}),
    claude_marker("marker", "snapshot"),
)
METADATA = json.dumps({"type": "atis-latch", "sessionId": SESSION})


def _write(path: Path, *lines: str) -> None:
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")


def _append(path: Path, *lines: str) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


@pytest.fixture
async def source(tmp_path):
    root = tmp_path / "projects"
    path = root / "project" / f"{SESSION}.jsonl"
    path.parent.mkdir(parents=True)
    reader = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="claude",
        cwd=str(tmp_path),
        session_id=SESSION,
        session_provenance=TranscriptProvenance.EXACT,
        known_location=str(path),
    )
    try:
        yield path, reader
    finally:
        await reader.aclose()


@pytest.fixture
def job_observer(registry, source):
    path, _ = source
    participant = registry.create_spawned(harness="claude", cwd=str(path.parent))
    jobs = JobManager(registry.store)
    jobs.create(handle="first", caller_id="caller", target_id=participant.id, kind="send")
    return Observer(registry, harnesses={}, jobs=jobs), participant.id, jobs


async def test_fast_first_reply_finishes_at_attach_without_replaying_history(source, job_observer):
    path, reader = source
    daemon, pid, jobs = job_observer
    assert (await reader.read()).waiting
    _write(path, *INITIAL, *ANSWER, METADATA)

    batch = await reader.read()
    assert daemon._accept_attachment(pid, reader, batch)

    assert (jobs.get("first").state, jobs.get("first").result) == (JobState.DONE, "DONE")
    assert batch.attached.point.records == len(INITIAL) + len(ANSWER)
    assert reader.offset == path.stat().st_size
    assert (await reader.read()).events == ()


async def test_held_first_reply_seals_after_uuidless_metadata(source, job_observer):
    path, reader = source
    daemon, pid, jobs = job_observer
    _write(path, *INITIAL, METADATA)
    assert daemon._accept_attachment(pid, reader, await reader.read())
    assert jobs.get("first").state == JobState.RUNNING

    _append(path, *ANSWER)
    batch = await reader.read()
    apply_batch(daemon, pid, batch, QuietClock(), TurnAccumulator(), source=reader)

    assert [event.raw_text for event in batch.events if event.turn_end] == ["DONE"]
    assert (jobs.get("first").state, jobs.get("first").result) == (JobState.DONE, "DONE")


@pytest.mark.parametrize("unknown", [False, True], ids=["exact_floor", "unknown_floor"])
async def test_metadata_does_not_lift_an_old_answer_past_resume_floor(
    registry, source, job_observer, unknown
):
    path, reader = source
    daemon, pid, jobs = job_observer
    _write(path, *INITIAL, *ANSWER)
    floor = reader._observer.stream_floor(str(path))
    participant = registry.store.get_participant(pid)
    participant.resume_floor = UNKNOWN_FLOOR if unknown else encode_floor(floor)
    registry.store.upsert_participant(participant)
    _append(path, METADATA)

    batch = await reader.read()
    assert daemon._accept_attachment(pid, reader, batch)

    assert batch.attached.last_event.turn_end
    assert batch.attached.point == floor
    assert jobs.get("first").state == JobState.RUNNING
    assert jobs.get("first").result is None

    _append(
        path,
        claude_record("new_user", "marker", "user", message={"content": "again"}),
        claude_fragment("new_answer", "new_user", {"type": "text", "text": "NEW"}, "msg_new"),
        claude_marker("new_marker", "new_answer"),
    )
    apply_batch(daemon, pid, await reader.read(), QuietClock(), TurnAccumulator(), source=reader)
    assert (jobs.get("first").state, jobs.get("first").result) == (JobState.DONE, "NEW")


@pytest.mark.parametrize("change", ["rewrite", "truncate", "replace"])
async def test_file_changes_during_context_read_discard_the_candidate(source, monkeypatch, change):
    path, reader = source
    _write(path, *INITIAL, *ANSWER, METADATA)
    original_open = Path.open
    changed = False

    class ChangingReader:
        def __init__(self, handle):
            self.handle = handle
            self.seeking = False

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def seek(self, offset):
            self.seeking = True
            return self.handle.seek(offset)

        def read(self, size):
            nonlocal changed
            raw = self.handle.read(size)
            if self.seeking and not changed:
                changed = True
                if change == "replace":
                    replacement = path.with_suffix(".replacement")
                    _write(replacement, *INITIAL, *ANSWER, METADATA)
                    replacement.replace(path)
                else:
                    stat = path.stat()
                    with original_open(path, "r+b") as target:
                        if change == "truncate":
                            target.truncate(0)
                        else:
                            data = target.read().replace(b"DONE", b"EVIL")
                            target.seek(0)
                            target.write(data)
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1))
            return raw

    @contextmanager
    def open_with_change(target, *args, **kwargs):
        with original_open(target, *args, **kwargs) as handle:
            yield ChangingReader(handle) if target == path and args == ("rb",) else handle

    monkeypatch.setattr(Path, "open", open_with_change)
    batch = await reader.read()

    assert batch.attached is None
    assert batch.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE
    assert changed
    _write(path, *INITIAL, *ANSWER, METADATA)
    recovered = await reader.read()
    assert recovered.attached.last_event.raw_text == "DONE"
    reader.commit_attachment()


async def test_replacement_during_context_parse_is_not_accepted(source, monkeypatch):
    path, reader = source
    _write(path, *INITIAL, *ANSWER, METADATA)
    parse = reader._observer.parse

    def replacing_parse(line, index, **kwargs):
        if index == 0:
            replacement = path.with_suffix(".replacement")
            _write(replacement, *INITIAL, *ANSWER, METADATA)
            replacement.replace(path)
        return parse(line, index, **kwargs)

    monkeypatch.setattr(reader._observer, "parse", replacing_parse)
    batch = await reader.read()
    assert batch.attached is None
    assert batch.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE

    monkeypatch.setattr(reader._observer, "parse", parse)
    assert (await reader.read()).attached.last_event.raw_text == "DONE"
    reader.commit_attachment()


async def test_cancelled_priming_can_retry_without_a_pending_candidate(source, monkeypatch):
    path, reader = source
    _write(path, *INITIAL, *ANSWER, METADATA)
    parse = reader._observer.parse
    task = asyncio.current_task()

    def cancelling_parse(line, index, **kwargs):
        if index == 0:
            task.cancel()
        return parse(line, index, **kwargs)

    monkeypatch.setattr(reader._observer, "parse", cancelling_parse)
    with pytest.raises(asyncio.CancelledError):
        await reader.read()

    monkeypatch.setattr(reader._observer, "parse", parse)
    assert (await reader.read()).attached.last_event.raw_text == "DONE"
    reader.commit_attachment()


async def test_incomplete_attach_record_is_retried_without_losing_its_prefix(source):
    path, reader = source
    _write(path, *INITIAL, METADATA)
    split = len(ANSWER[0]) // 2
    with path.open("a", encoding="utf-8") as handle:
        handle.write(ANSWER[0][:split])

    batch = await reader.read()
    assert batch.attached is None
    assert batch.error_code == TRANSCRIPT_SOURCE_UNAVAILABLE_CODE

    with path.open("a", encoding="utf-8") as handle:
        handle.write(ANSWER[0][split:] + "\n")
    _append(path, *ANSWER[1:], METADATA)
    assert (await reader.read()).attached.last_event.raw_text == "DONE"
    reader.commit_attachment()


async def test_large_transcript_primes_only_a_bounded_suffix(source, monkeypatch):
    path, reader = source
    lines = [METADATA] * (SEAL_NODE_LIMIT * 32) + [*INITIAL, *ANSWER, METADATA]
    _write(path, *lines)
    parse = reader._observer.parse
    indices = []
    original_open = Path.open
    reads = []

    class TrackingReader:
        def __init__(self, handle, positions):
            self.handle = handle
            self.positions = positions

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def read(self, size):
            offset = self.handle.tell()
            raw = self.handle.read(size)
            self.positions.append((offset, len(raw)))
            return raw

        def readline(self, size):
            offset = self.handle.tell()
            raw = self.handle.readline(size)
            self.positions.append((offset, len(raw)))
            return raw

    @contextmanager
    def tracking_open(target, *args, **kwargs):
        with original_open(target, *args, **kwargs) as handle:
            if target == path and args == ("rb",):
                positions = []
                reads.append(positions)
                yield TrackingReader(handle, positions)
            else:
                yield handle

    def counting_parse(line, index, **kwargs):
        indices.append(index)
        return parse(line, index, **kwargs)

    monkeypatch.setattr(reader._observer, "parse", counting_parse)
    monkeypatch.setattr(Path, "open", tracking_open)
    batch = await reader.read()

    assert batch.attached.last_event.raw_text == "DONE"
    assert 0 < len(indices) <= SEAL_NODE_LIMIT
    assert min(indices) >= len(lines) - SEAL_NODE_LIMIT
    assert batch.attached.last_event.raw_index == len(lines) - 2
    context_reads = reads[-1]
    assert context_reads and min(offset for offset, _ in context_reads) > 0
    assert sum(size for _, size in context_reads) <= HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES
    reader.commit_attachment()
    assert (await reader.read()).events == ()


@pytest.mark.parametrize("chunk_bytes", [7, 64, 4096])
def test_tail_chunks_preserve_record_indices_and_byte_offsets(tmp_path, monkeypatch, chunk_bytes):
    path = tmp_path / "records.jsonl"
    lines = [json.dumps({"text": "é" * index}, ensure_ascii=False) for index in range(20)]
    _write(path, *lines)
    stat = path.stat()
    point = StreamPoint(records=len(lines), size=stat.st_size, dev=stat.st_dev, ino=stat.st_ino)
    monkeypatch.setattr(claude_source, "SEAL_NODE_LIMIT", 3)
    monkeypatch.setattr(claude_source, "HARNESS_TRANSCRIPT_SCAN_CHUNK_BYTES", chunk_bytes)

    context = claude_source._attachment_context(path, point, stat.st_mtime_ns)

    offsets = [len("\n".join(lines[: index + 1]).encode()) + 1 for index in range(len(lines))]
    assert context == [(index, offsets[index], lines[index]) for index in range(17, 20)]
