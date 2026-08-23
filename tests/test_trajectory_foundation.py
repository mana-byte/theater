"""Focused tests for the trajectory domain and additive harness seams."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Batch, History, Source
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.transcript.observer import TranscriptObserver, open_participant_source
from theater.harness.transcript.source import TranscriptSource
from theater.trajectory import (
    ContentFormat,
    ContentPreview,
    DetailField,
    GroupKind,
    PanelState,
    PanelStateInfo,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryValidationError,
    bounded_preview,
    event_to_record,
    fallback_record_id,
    group_records,
    merge_records,
)


def make_record(
    record_id: str,
    *,
    revision: int = 0,
    raw_index: int = 0,
    turn_id: str | None = None,
    step_id: str | None = None,
    details: tuple[DetailField, ...] = (),
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id="participant",
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="baseline",
        summary="answer",
        status=TrajectoryStatus.COMPLETED,
        raw_index=raw_index,
        turn_id=turn_id,
        step_id=step_id,
        details=details,
    )


def test_canonical_record_is_immutable_and_strictly_round_trips() -> None:
    record = make_record("epoch:1:0", details=(DetailField.from_text("output", "done"),))
    assert TrajectoryRecord.from_wire(record.to_wire()) == record
    with pytest.raises(FrozenInstanceError):
        record.summary = "changed"  # type: ignore[misc]

    invalid = record.to_wire()
    invalid["plugin_payload"] = {"not": "a wire field"}
    with pytest.raises(TrajectoryValidationError):
        TrajectoryRecord.from_wire(invalid)

    invalid_detail = record.to_wire()
    invalid_detail["details"] = [{"name": "output", "value": {"arbitrary": True}}]
    with pytest.raises(TrajectoryValidationError):
        TrajectoryRecord.from_wire(invalid_detail)


def test_utf8_preview_keeps_safe_head_tail_and_exact_omission() -> None:
    original = "é" * 20_000
    preview = bounded_preview(original)
    marker = f"… {preview.omitted_bytes} bytes omitted …"
    shown = preview.text.replace(marker, "", 1)
    assert len(preview.text.encode("utf-8")) <= 16 * 1024
    assert preview.omitted_bytes == len(original.encode("utf-8")) - len(shown.encode("utf-8"))
    assert shown.startswith("é") and shown.endswith("é")
    assert marker in preview.text

    unsafe = bounded_preview("\x1b[31m[bold]\x00")
    assert "\x1b" not in unsafe.text
    assert "\x00" not in unsafe.text
    assert "\\[bold]" in unsafe.text
    with pytest.raises(TrajectoryValidationError):
        ContentPreview("[bold]")


def test_record_detail_fields_obey_field_and_aggregate_byte_caps() -> None:
    details = tuple(
        DetailField.from_text(str(index), "x" * (20 * 1024), format=ContentFormat.TEXT)
        for index in range(3)
    )
    record = make_record("r", details=details)
    assert all(detail.preview.encoded_bytes <= 16 * 1024 for detail in record.details)
    assert sum(detail.preview.encoded_bytes for detail in record.details) <= 32 * 1024
    assert any(detail.preview.omitted_bytes for detail in record.details)


def test_projection_identity_revision_and_grouping_are_deterministic() -> None:
    assert fallback_record_id("trusted-epoch", 4, 2) == "trusted-epoch:4:2"
    event = Event(
        kind=EventKind.ASSISTANT, text="done", raw_index=4, turn_id="turn-1", turn_end=True
    )
    record = event_to_record(event, participant_id="p", source_epoch="trusted-epoch")
    assert record.record_id == "trusted-epoch:4:0"

    old = make_record("native", revision=1)
    new = make_record("native", revision=2)
    assert merge_records((new,), (old,)) == (new,)

    groups = group_records(
        (
            make_record("unplaced", raw_index=0),
            make_record("step", raw_index=1, turn_id="t1", step_id="s1"),
            make_record("turn", raw_index=2, turn_id="t1"),
        )
    )
    assert groups[0].kind is GroupKind.BETWEEN_TURNS
    assert groups[1].kind is GroupKind.TURN
    assert groups[1].children[0].kind is GroupKind.STEP


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


async def test_default_parse_record_calls_legacy_parse_once() -> None:
    path = Path("/tmp/unused-transcript")
    observer = LegacyCountingObserver(path)
    parsed = observer.parse_record("line", 3)
    assert len(parsed.events) == 1
    assert parsed.trajectory == ()
    assert observer.parse_calls == 1


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


async def test_default_history_page_is_honest_about_older_history() -> None:
    page = await HistoryOnlySource().history_page(limit=2)
    assert [event.text for event in page.events] == ["history"]
    assert page.pinned is True
    assert page.has_older is False
    assert page.older_cursor is None
    unavailable = await HistoryOnlySource().history_page(before="opaque", limit=2)
    assert unavailable.error_code == "history_paging_unavailable"
    assert unavailable.has_older is False


def test_panel_state_wire_round_trip() -> None:
    page = TrajectoryPage(panel_state=PanelStateInfo(PanelState.WAITING, "waiting for transcript"))
    assert TrajectoryPage.from_wire(page.to_wire()) == page
