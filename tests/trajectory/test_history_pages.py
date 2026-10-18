"""Transcript history pages and cursors, record parsing seams and legacy observer compatibility."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from theater.constants.trajectory import (
    TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES,
    TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES,
)
from theater.daemon.trajectory.project import event_to_record
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import (
    Batch,
    History,
    HistoryPage,
    Source,
    SourceContractError,
)
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.transcript.observer import TranscriptObserver, open_participant_source
from theater.harness.transcript.source import TranscriptSource
from theater.trajectory import (
    TrajectoryKind,
)


def test_history_page_rejects_silent_output_truncation() -> None:
    events = tuple(Event(kind=EventKind.ASSISTANT, text=str(index)) for index in range(201))
    with pytest.raises(SourceContractError):
        HistoryPage(events=events)


class LegacyCountingObserver(TranscriptObserver):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.parse_calls = 0

    def find_transcript(
        self, *, cwd: str, session_id: str | None = None, after: float | None = None
    ):
        return self.path

    def session_id(self, transcript: Path) -> str | None:
        return "session"

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        self.parse_calls += 1
        return [Event(kind=EventKind.ASSISTANT, text=line, raw_index=index)]

    def is_idle_screen(self, capture: str) -> bool:
        return False


class RichObserver(LegacyCountingObserver):
    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        self.parse_calls += 1
        return ParsedRecord(
            events=(Event(kind=EventKind.ASSISTANT, text=line, raw_index=index),),
            trajectory=(
                TrajectoryFact(
                    kind=TrajectoryKind.ASSISTANT,
                    source="rich-test",
                    native_id=f"native-{index}",
                    raw_index=index,
                ),
            ),
        )


class OrderedObserver(LegacyCountingObserver):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.seen: list[str] = []

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        self.seen.append(line)
        return ParsedRecord(
            events=(Event(kind=EventKind.ASSISTANT, text=line, raw_index=index),),
        )


class MultiRecordObserver(LegacyCountingObserver):
    def __init__(self, path: Path, output_count: int = 2) -> None:
        super().__init__(path)
        self.output_count = output_count

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        self.parse_calls += 1
        return ParsedRecord(
            events=tuple(
                Event(kind=EventKind.ASSISTANT, text=f"{line}-event-{ordinal}", raw_index=index)
                for ordinal in range(self.output_count)
            ),
            trajectory=tuple(
                TrajectoryFact(
                    kind=TrajectoryKind.ASSISTANT,
                    source="multi-test",
                    native_id=f"{line}-fact-{ordinal}",
                    raw_index=index,
                    event_ordinal=ordinal,
                )
                for ordinal in range(self.output_count)
            ),
        )


class SuppressingObserver(LegacyCountingObserver):
    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        self.parse_calls += 1
        event = Event(kind=EventKind.ASSISTANT, text=line, raw_index=index)
        return ParsedRecord(events=(event,), trajectory_events=())


class RawObserver(LegacyCountingObserver):
    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        self.parse_calls += 1
        return [
            Event(
                kind=EventKind.ASSISTANT,
                text=line,
                raw_text="[raw]\\literal " + "x" * (20 * 1024),
                raw_index=index,
            )
        ]


class InvalidRecordObserver(LegacyCountingObserver):
    def parse_record(self, line: str, index: int, *, clip_text: bool = True):
        self.parse_calls += 1
        return [Event(kind=EventKind.ASSISTANT, text=line, raw_index=index)]


async def test_default_parse_record_calls_legacy_parse_once() -> None:
    path = Path("/tmp/unused-transcript")
    observer = LegacyCountingObserver(path)
    parsed = observer.parse_record("line", 3)
    assert len(parsed.events) == 1
    assert parsed.trajectory == ()
    assert observer.parse_calls == 1


async def test_invalid_parse_record_result_raises_without_legacy_fallback(tmp_path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text("one\n", encoding="utf-8")
    observer = InvalidRecordObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    with pytest.raises(SourceContractError):
        await source.read()
    assert observer.parse_calls == 1


async def test_history_page_parses_selected_records_in_transcript_order(tmp_path) -> None:
    path = tmp_path / "ordered.jsonl"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    observer = OrderedObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))

    page = await source.history_page(limit=3)

    assert observer.seen == ["two", "three", "four"]
    assert [event.text for event in page.events] == ["two", "three", "four"]


async def test_transcript_live_reads_emit_facts_and_history_pages_do_not_move_cursor(
    tmp_path,
) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    observer = RichObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))

    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    before_page = (source.path, source.offset, source.index, source.mtime)

    page = await source.history_page(limit=1)
    assert [event.text for event in page.events] == ["three"]
    assert [fact.native_id for fact in page.trajectory] == ["native-2"]
    assert page.has_older is True
    assert page.older_cursor is not None
    assert (source.path, source.offset, source.index, source.mtime) == before_page

    older = await source.history_page(before=page.older_cursor, limit=1)
    assert [event.text for event in older.events] == ["two"]
    assert older.has_older is True
    assert (source.path, source.offset, source.index, source.mtime) == before_page

    with path.open("a", encoding="utf-8") as handle:
        handle.write("four\n")
    live = await source.read()
    assert [event.text for event in live.events] == ["four"]
    assert [fact.native_id for fact in live.trajectory] == ["native-3"]
    assert observer.parse_calls == 4


async def test_transcript_source_keeps_control_events_out_of_opted_out_trajectory(tmp_path) -> None:
    path = tmp_path / "duplicate.jsonl"
    path.write_text("one\n", encoding="utf-8")
    source = TranscriptSource(SuppressingObserver(path), cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    page = await source.history_page(limit=1)
    assert [event.text for event in page.events] == ["one"]
    assert page.trajectory_events == ()

    with path.open("a", encoding="utf-8") as handle:
        handle.write("two\n")
    batch = await source.read()
    assert [event.text for event in batch.events] == ["two"]
    assert batch.trajectory_events == ()


async def test_history_pages_keep_complete_multi_output_records(tmp_path) -> None:
    path = tmp_path / "multi.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    observer = MultiRecordObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    pages = []
    page = await source.history_page(limit=2)
    while True:
        pages.append(page)
        if page.older_cursor is None:
            break
        page = await source.history_page(before=page.older_cursor, limit=2)

    events = [event.text for page in reversed(pages) for event in page.events]
    facts = [fact.native_id for page in reversed(pages) for fact in page.trajectory]
    assert events == [
        "one-event-0",
        "one-event-1",
        "two-event-0",
        "two-event-1",
        "three-event-0",
        "three-event-1",
    ]
    assert facts == [
        "one-fact-0",
        "one-fact-1",
        "two-fact-0",
        "two-fact-1",
        "three-fact-0",
        "three-fact-1",
    ]


async def test_history_page_reports_a_raw_record_over_limit(tmp_path) -> None:
    path = tmp_path / "oversized-output.jsonl"
    path.write_text("one\n", encoding="utf-8")
    observer = MultiRecordObserver(path, output_count=3)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    page = await source.history_page(limit=2)
    assert page.error_code == "history_record_too_large"
    assert page.older_cursor is None
    assert page.has_older is False


async def test_history_cursor_invalidates_after_rewrite_and_rotation(tmp_path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    observer = RichObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    page = await source.history_page(limit=1)
    assert page.older_cursor is not None

    path.write_text("rewritten\ntwo\nthree\n", encoding="utf-8")
    rewritten = await source.history_page(before=page.older_cursor, limit=1)
    assert rewritten.error_code == "history_cursor_invalid"

    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    page = await source.history_page(limit=1)
    assert page.older_cursor is not None
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_text("replacement\n", encoding="utf-8")
    replacement.replace(path)
    rotated = await source.history_page(before=page.older_cursor, limit=1)
    assert rotated.error_code == "history_cursor_invalid"


async def test_history_cursor_remains_a_v3_transcript_identity_snapshot(tmp_path) -> None:
    path = tmp_path / "cursor.jsonl"
    path.write_text("one\ntwo\n", encoding="utf-8")
    source = TranscriptSource(LegacyCountingObserver(path), cwd=str(tmp_path))

    page = await source.history_page(limit=1)

    assert page.cursor is not None
    encoded = page.cursor.removeprefix("trj1.")
    payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    assert set(payload) == {"v", "path", "identity", "end", "end_index"}
    assert payload["v"] == 3
    assert set(payload["identity"]) == {
        "dev",
        "ino",
        "size",
        "mtime_ns",
        "ctime_ns",
        "head",
        "boundary_offset",
        "boundary",
    }


async def test_history_cursor_rejects_a_shrunk_transcript(tmp_path) -> None:
    path = tmp_path / "shrunken.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    source = TranscriptSource(LegacyCountingObserver(path), cwd=str(tmp_path))
    page = await source.history_page(limit=1)
    assert page.older_cursor is not None

    path.write_text("one\n", encoding="utf-8")

    older = await source.history_page(before=page.older_cursor, limit=1)
    assert older.error_code == "history_cursor_invalid"


async def test_history_cursor_survives_append(tmp_path) -> None:
    path = tmp_path / "append.jsonl"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    observer = LegacyCountingObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    page = await source.history_page(limit=1)
    assert page.older_cursor is not None

    with path.open("a", encoding="utf-8") as handle:
        handle.write("four\n")

    older = await source.history_page(before=page.older_cursor, limit=1)
    assert older.error_code is None
    assert [event.text for event in older.events] == ["two"]


async def test_history_page_work_is_bounded_to_reverse_window(tmp_path, monkeypatch) -> None:
    path = tmp_path / "large.jsonl"
    path.write_text("".join(f"record-{index}\n" for index in range(200_000)), encoding="utf-8")
    observer = LegacyCountingObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    bytes_read = [0]
    real_open = Path.open

    class CountingFile:
        def __init__(self, handle) -> None:
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, *args):
            value = self.handle.read(*args)
            bytes_read[0] += len(value)
            return value

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def counted_open(file_path, *args, **kwargs):
        return CountingFile(real_open(file_path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", counted_open)
    page = await source.history_page(limit=1)
    assert [event.text for event in page.events] == ["record-199999"]
    assert bytes_read[0] < path.stat().st_size


@pytest.mark.parametrize(
    ("line_bytes", "error_code"),
    [
        (TRAJECTORY_TRANSCRIPT_HISTORY_WINDOW_BYTES + 32, None),
        (TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES + 32, "history_record_too_large"),
    ],
)
async def test_history_page_bounds_oversized_records(tmp_path, line_bytes, error_code) -> None:
    path = tmp_path / "oversized.jsonl"
    path.write_bytes(b"x" * line_bytes + b"\nsmall\n")
    observer = LegacyCountingObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    newest = await source.history_page(limit=1)
    assert newest.older_cursor is not None

    older = await source.history_page(before=newest.older_cursor, limit=1)
    assert older.error_code == error_code
    if error_code is None:
        assert older.events
        assert older.older_cursor is None
    else:
        assert older.events == ()
        assert older.older_cursor is None


async def test_history_page_does_not_return_a_self_cursor_for_a_partial_record(tmp_path) -> None:
    path = tmp_path / "partial.jsonl"
    path.write_bytes(b"partial record")
    observer = LegacyCountingObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()

    page = await source.history_page(limit=1)
    assert page.events == ()
    assert page.has_older is False
    assert page.older_cursor is None


async def test_baseline_fallback_identity_uses_source_offsets_across_reads(tmp_path) -> None:
    path = tmp_path / "coordinates.jsonl"
    path.write_text("one\n", encoding="utf-8")
    observer = LegacyCountingObserver(path)
    watcher = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await watcher.read()
    assert attached.attached is not None
    watcher.commit_attachment()
    with path.open("a", encoding="utf-8") as handle:
        handle.write("two\n")
    live = await watcher.read()
    live_event = live.events[0]
    live_record = event_to_record(live_event, participant_id="p", source_epoch="epoch")

    with path.open("a", encoding="utf-8") as handle:
        handle.write("three\n")
    fresh = TranscriptSource(LegacyCountingObserver(path), cwd=str(tmp_path))
    newest = await fresh.history_page(limit=1)
    assert newest.older_cursor is not None
    older = await fresh.history_page(before=newest.older_cursor, limit=1)
    historical_event = older.events[0]
    historical_record = event_to_record(historical_event, participant_id="p", source_epoch="epoch")
    assert live_event.raw_index == historical_event.raw_index
    assert live_event.source_offset == historical_event.source_offset
    assert live_record.record_id == historical_record.record_id


async def test_history_page_does_not_retain_unbounded_event_raw_text(tmp_path) -> None:
    path = tmp_path / "raw.jsonl"
    path.write_text("record\n", encoding="utf-8")
    observer = RawObserver(path)
    source = TranscriptSource(observer, cwd=str(tmp_path))
    attached = await source.read()
    assert attached.attached is not None
    source.commit_attachment()
    page = await source.history_page(limit=1)
    assert page.events[0].raw_text is not None
    assert page.events[0].raw_text.startswith("[raw]\\literal")
    assert len(page.events[0].raw_text.encode("utf-8")) <= 16 * 1024


class LegacySource(Source):
    async def read(self) -> Batch:
        return Batch(events=(Event(kind=EventKind.USER, text="legacy"),))


class OldStyleObserver:
    def __init__(self) -> None:
        self.source = LegacySource()

    def open_source(
        self, *, cwd: str | None, session_id: str | None = None, after: float | None = None
    ):
        return self.source


def test_old_style_observer_and_source_remain_compatible() -> None:
    observer = OldStyleObserver()
    assert (
        open_participant_source(
            observer,
            participant_id="p",
            cwd=None,
        )
        is observer.source
    )


class HistoryOnlySource(Source):
    async def read(self) -> Batch:
        return Batch()

    async def history(self, *, last_n: int) -> History:
        return History(events=(Event(kind=EventKind.USER, text="history"),), pinned=True)


class UnboundedHistorySource(Source):
    async def read(self) -> Batch:
        return Batch()

    async def history(self, *, last_n: int) -> History:
        return History(
            events=tuple(
                Event(kind=EventKind.ASSISTANT, text="x" * 20_000, raw_text="y" * 20_000)
                for _ in range(500)
            )
        )


async def test_default_history_page_is_honest_about_older_history() -> None:
    page = await HistoryOnlySource().history_page(limit=2)
    assert [event.text for event in page.events] == ["history"]
    assert page.pinned is True
    assert page.has_older is False
    assert page.older_cursor is None
    unavailable = await HistoryOnlySource().history_page(before="opaque", limit=2)
    assert unavailable.error_code == "history_paging_unavailable"
    assert unavailable.has_older is False


async def test_default_history_page_bounds_legacy_history() -> None:
    page = await UnboundedHistorySource().history_page(limit=2)
    assert len(page.events) == 2
    assert all(len(event.text.encode("utf-8")) <= 16 * 1024 for event in page.events)
    assert all(event.raw_text is not None for event in page.events)
    assert all(len(event.raw_text.encode("utf-8")) <= 16 * 1024 for event in page.events)
