"""Transcript history pages are bounded, cursor-bound and carry retained prompts."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.opencode_recorder_v2 import RecorderV2
from tests.support.opencode_recorder_v2 import text as opencode_text
from tests.support.shipped import OpenCodeObserver
from tests.support.transcript_pages import StaticSource, page, pager
from theater.constants.daemon import TRANSCRIPT_READ_RESPONSE_MAX_BYTES
from theater.daemon import methods as methods_mod
from theater.daemon import transcript_read as transcript_read_mod
from theater.daemon.transcript_paging import TranscriptCursorError, TranscriptPager
from theater.harness import Event, EventKind
from theater.harness.contracts.events import TokenUsage
from theater.harness.contracts.source import HistoryPage
from theater.harness.observation import TranscriptObserver
from theater.harness.transcript.observer import open_participant_source
from theater.models import BadRequest, Job, Tier


async def test_newest_page_is_bounded_and_chronological() -> None:
    source = StaticSource(
        {
            None: page(
                (
                    Event(kind=EventKind.USER, text="old", raw_index=1),
                    Event(kind=EventKind.ASSISTANT, text="new", raw_index=2, turn_end=True),
                ),
                cursor="newest",
                older_cursor="older",
            ),
            "older": page(
                (Event(kind=EventKind.USER, text="oldest", raw_index=0),),
                cursor="oldest",
            ),
        }
    )

    first = await pager(source).read()

    assert [chunk.text for chunk in first.events] == ["old", "new"]
    assert [chunk.event.raw_index for chunk in first.events] == [1, 2]
    assert first.cursor is None
    assert first.has_more is True
    assert first.truncated is False
    assert source.calls == [(None, None, True)]
    wire = first.to_wire(target="p1")
    assert wire["cursor"] is None
    assert wire["next_cursor"] is not None
    assert wire["has_more"] is True
    assert wire["truncated"] is False
    assert set(wire["events"][0]) == {
        "event_position",
        "index",
        "role",
        "text",
        "tool_name",
        "turn_end",
        "turn_terminal",
        "text_start_byte",
        "text_end_byte",
        "text_total_bytes",
        "reaches_text_start",
    }
    assert (
        len(json.dumps(wire, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        <= TRANSCRIPT_READ_RESPONSE_MAX_BYTES
    )


async def test_pagination_has_no_overlap_and_ends_at_history() -> None:
    source = StaticSource(
        {
            None: page(
                (Event(kind=EventKind.ASSISTANT, text="new", raw_index=2),),
                cursor="newest",
                older_cursor="older",
            ),
            "older": page(
                (Event(kind=EventKind.ASSISTANT, text="old", raw_index=1),),
                cursor="older-page",
            ),
        }
    )

    first = await pager(source).read()
    second = await pager(source).read(first.next_cursor)

    assert [event.text for event in first.events + second.events] == ["new", "old"]
    assert {event.event.raw_index for event in first.events}.isdisjoint(
        {event.event.raw_index for event in second.events}
    )
    assert second.next_cursor is None
    assert second.has_more is False


async def test_truncated_marks_a_response_budget_cut_source_page() -> None:
    source = StaticSource(
        {
            None: page(
                (
                    Event(kind=EventKind.ASSISTANT, text="old", raw_index=1),
                    Event(kind=EventKind.ASSISTANT, text="n" * 15_700, raw_index=2),
                ),
                cursor="newest",
            )
        }
    )

    result = await pager(source).read()

    assert result.truncated is True
    assert [chunk.event.raw_index for chunk in result.events] == [2]
    assert result.next_cursor is not None


async def test_one_oversized_event_is_retrievable_in_reverse_chunks() -> None:
    text = "".join(
        chr(0x400 + index % 32) for index in range(TRANSCRIPT_READ_RESPONSE_MAX_BYTES * 3)
    )
    source = StaticSource(
        {None: page((Event(kind=EventKind.ASSISTANT, text=text, raw_index=7),), cursor="newest")}
    )

    chunks = []
    current = await pager(source).read()
    while True:
        chunks.extend(current.events)
        assert (
            len(
                json.dumps(
                    current.to_wire(target="p1"), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            <= TRANSCRIPT_READ_RESPONSE_MAX_BYTES
        )
        if current.next_cursor is None:
            break
        current = await pager(source).read(current.next_cursor)

    assert len(chunks) > 1
    assert all(chunk.event.raw_index == 7 for chunk in chunks)
    assert chunks[-1].reaches_text_start is True
    assert "".join(chunk.text for chunk in reversed(chunks)) == text
    ordered = sorted(chunks, key=lambda chunk: chunk.start)
    assert [(chunk.start, chunk.end) for chunk in ordered] == [
        (start, end)
        for start, end in zip(
            (0, *(chunk.end for chunk in ordered[:-1])),
            (chunk.end for chunk in ordered),
            strict=True,
        )
    ]


async def test_four_byte_emoji_event_is_retrievable_at_production_budget() -> None:
    text = "😀" * 50_000
    source = StaticSource(
        {None: page((Event(kind=EventKind.ASSISTANT, text=text, raw_index=7),), cursor="newest")}
    )

    chunks = []
    current = await pager(source).read()
    while True:
        chunks.extend(current.events)
        assert current.events
        assert (
            len(
                json.dumps(
                    current.to_wire(target="p1"), ensure_ascii=False, separators=(",", ":")
                ).encode("utf-8")
            )
            <= TRANSCRIPT_READ_RESPONSE_MAX_BYTES
        )
        if current.next_cursor is None:
            break
        current = await pager(source).read(current.next_cursor)

    assert len(chunks) > 1
    assert all(chunk.event.raw_index == 7 for chunk in chunks)
    assert "".join(chunk.text for chunk in reversed(chunks)) == text


async def test_oversized_event_on_an_older_page_continues() -> None:
    text = "é" * (TRANSCRIPT_READ_RESPONSE_MAX_BYTES * 2)
    source = StaticSource(
        {
            None: page(
                (Event(kind=EventKind.ASSISTANT, text="newest", raw_index=2),),
                cursor="newest",
                older_cursor="older",
            ),
            "older": page(
                (Event(kind=EventKind.ASSISTANT, text=text, raw_index=1),), cursor="older-page"
            ),
        }
    )

    first = await pager(source).read()
    chunks = []
    current = await pager(source).read(first.next_cursor)
    while True:
        chunks.extend(current.events)
        if current.next_cursor is None:
            break
        current = await pager(source).read(current.next_cursor)

    assert "".join(chunk.text for chunk in reversed(chunks)) == text


async def test_cursor_is_versioned_target_bound_and_stale_safe() -> None:
    source = StaticSource(
        {
            None: page(
                (Event(kind=EventKind.ASSISTANT, text="x" * 30_000, raw_index=1),),
                cursor="newest",
            )
        }
    )
    first = await pager(source).read()
    assert first.next_cursor is not None

    with pytest.raises(TranscriptCursorError, match=r"malformed.*omit cursor"):
        await pager(source).read("not-a-theater-cursor")
    with pytest.raises(TranscriptCursorError, match=r"another participant.*omit cursor"):
        await pager(source, target="p2").read(first.next_cursor)

    source.pages[None] = page(
        (Event(kind=EventKind.ASSISTANT, text="changed" * 30_000, raw_index=1),),
        cursor="changed",
    )
    with pytest.raises(TranscriptCursorError, match=r"changed.*omit cursor"):
        await pager(source).read(first.next_cursor)


async def test_source_cursor_invalid_requires_restart() -> None:
    class CursorInvalidSource(StaticSource):
        async def history_page(self, **kwargs: object) -> HistoryPage:
            if kwargs.get("snapshot") is not None:
                return HistoryPage(
                    error_code="history_cursor_invalid",
                    error="history cursor no longer matches the transcript",
                )
            return await super().history_page(**kwargs)

    source = CursorInvalidSource(
        {
            None: page(
                (Event(kind=EventKind.ASSISTANT, text="x" * 30_000, raw_index=1),),
                cursor="newest",
            )
        }
    )
    first = await pager(source).read()
    assert first.next_cursor is not None

    with pytest.raises(TranscriptCursorError, match=r"no longer matches.*omit cursor"):
        await pager(source).read(first.next_cursor)


class LineObserver(TranscriptObserver):
    def __init__(self, path: Path) -> None:
        self.path = path

    def find_transcript(
        self, *, cwd: str, session_id: str | None = None, after: float | None = None
    ) -> Path:
        return self.path

    def session_id(self, transcript: Path) -> str | None:
        return "session"

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        return [
            Event(
                kind=EventKind.ASSISTANT,
                text=line if line != "skip" else "",
                raw_index=index,
                usage=TokenUsage() if line == "skip" else None,
            )
        ]

    def is_idle_screen(self, capture: str) -> bool:
        return False


async def test_file_source_uses_bounded_history_pages(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text("first\nmiddle\nlatest\n", encoding="utf-8")
    source = open_participant_source(
        LineObserver(path),
        participant_id="p1",
        cwd=str(tmp_path),
    )
    try:
        first = await pager(source).read()
    finally:
        await source.aclose()

    assert [chunk.text for chunk in first.events] == ["first", "middle", "latest"]
    assert first.next_cursor is None


async def test_file_source_advances_across_filtered_empty_pages(tmp_path: Path) -> None:
    path = tmp_path / "transcript.jsonl"
    path.write_text("visible\n" + "skip\n" * 24, encoding="utf-8")
    source = open_participant_source(
        LineObserver(path),
        participant_id="p1",
        cwd=str(tmp_path),
    )
    try:
        result = await pager(source).read()
    finally:
        await source.aclose()

    assert [chunk.text for chunk in result.events] == ["visible"]
    assert result.next_cursor is None


async def test_file_snapshot_replays_split_event_after_append(tmp_path: Path) -> None:
    text = "é" * (TRANSCRIPT_READ_RESPONSE_MAX_BYTES * 2)
    path = tmp_path / "transcript.jsonl"
    path.write_text(text + "\n", encoding="utf-8")
    source = open_participant_source(
        LineObserver(path),
        participant_id="p1",
        cwd=str(tmp_path),
    )
    try:
        first = await pager(source).read()
        with path.open("a", encoding="utf-8") as handle:
            handle.write("newer\n")
        chunks = list(first.events)
        current = first
        while current.next_cursor is not None:
            current = await pager(source).read(current.next_cursor)
            chunks.extend(current.events)
    finally:
        await source.aclose()

    assert "newer" not in "".join(chunk.text for chunk in chunks)
    assert "".join(chunk.text for chunk in reversed(chunks)) == text


async def test_opencode_source_uses_the_same_pager(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    recorder = RecorderV2(tmp_path / "opencode.db", "session", str(workdir))
    try:
        recorder.step(
            "message-1",
            recorder.tick(),
            [opencode_text("é" * 20_000)],
            finish="stop",
            times={"completed": recorder.tick()},
        )
        source = OpenCodeObserver(db=recorder.path).open_source(
            cwd=str(workdir), session_id="session"
        )
        try:
            first = await pager(source).read()
            assert first.events
            assert first.events[0].text
            assert first.next_cursor is not None
        finally:
            await source.aclose()
    finally:
        recorder.conn.close()


async def test_opencode_snapshot_excludes_a_newer_message_between_chunks(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    text = "é" * (TRANSCRIPT_READ_RESPONSE_MAX_BYTES * 2)
    recorder = RecorderV2(tmp_path / "opencode.db", "session", str(workdir))
    try:
        recorder.step(
            "message-1",
            recorder.tick(),
            [opencode_text(text)],
            finish="stop",
            times={"completed": recorder.tick()},
        )
        source = OpenCodeObserver(db=recorder.path).open_source(
            cwd=str(workdir), session_id="session"
        )
        try:
            first = await pager(source).read()
            recorder.step(
                "message-2",
                recorder.tick(),
                [opencode_text("newer")],
                finish="stop",
                times={"completed": recorder.tick()},
            )
            chunks = list(first.events)
            current = first
            while current.next_cursor is not None:
                current = await pager(source).read(current.next_cursor)
                chunks.extend(current.events)
        finally:
            await source.aclose()
    finally:
        recorder.conn.close()

    assert "newer" not in "".join(chunk.text for chunk in chunks)
    assert "".join(chunk.text for chunk in reversed(chunks)) == text


async def test_roles_filter_events_and_bind_the_cursor() -> None:
    source = StaticSource(
        {
            None: page(
                (
                    Event(kind=EventKind.ASSISTANT, text="answer", raw_index=1),
                    Event(kind=EventKind.TOOL_RESULT, text="x" * 30_000, raw_index=2),
                ),
                cursor="newest",
                older_cursor="older",
            ),
            "older": page((Event(kind=EventKind.USER, text="ask", raw_index=0),), cursor="o"),
        }
    )
    assistant = TranscriptPager(
        source, target="p1", event_filter=lambda _event: True, roles=("assistant",)
    )

    first = await assistant.read()

    assert [chunk.text for chunk in first.events] == ["answer"]
    assert first.next_cursor is not None
    assert (await assistant.read(first.next_cursor)).events == ()
    with pytest.raises(TranscriptCursorError, match=r"different roles.*omit cursor"):
        await pager(source).read(first.next_cursor)
    with pytest.raises(TranscriptCursorError, match=r"version.*omit cursor"):
        await pager(source).read("trc2." + first.next_cursor.removeprefix("trc3."))


async def test_huge_event_collapses_and_middle_cursor_stays_inside_it() -> None:
    huge = "".join(chr(0x61 + index % 26) for index in range(100 * 1024))
    source = StaticSource(
        {
            None: page(
                (
                    Event(kind=EventKind.USER, text="older", raw_index=0),
                    Event(kind=EventKind.TOOL_RESULT, text=huge, raw_index=1),
                    Event(kind=EventKind.ASSISTANT, text="newest", raw_index=2),
                ),
                cursor="newest",
            )
        }
    )
    collapsing = TranscriptPager(
        source, target="p1", event_filter=lambda _event: True, collapse_over=64 * 1024
    )

    first = await collapsing.read()

    assert [chunk.event.raw_index for chunk in first.events] == [0, 1, 2]
    collapsed = first.events[1].to_wire()
    head, tail = collapsed["text"], collapsed["tail"]
    assert huge.startswith(head) and huge.endswith(tail)
    assert len(head) == len(tail) == 6 * 1024
    assert collapsed["elided_bytes"] == len(huge) - len(head) - len(tail)
    assert first.next_cursor is None
    middle: list[str] = []
    cursor = collapsed["middle_cursor"]
    while cursor is not None:
        current = await collapsing.read(cursor)
        assert [chunk.event.raw_index for chunk in current.events] == [1]
        middle.extend(chunk.text for chunk in current.events)
        cursor = current.next_cursor
    assert head + "".join(reversed(middle)) + tail == huge


def _job(handle: str, target: str, kind: str, prompt: str | None, created_at: float) -> Job:
    return Job(handle, "p-parent", target, kind, prompt, "done", None, None, created_at, created_at)


async def test_newest_page_carries_retained_prompts_and_roles_are_validated(registry, monkeypatch):
    child = registry.register(harness="vibe", pane=None, cwd="/tmp")
    child = dataclasses.replace(child, tier=Tier.SPAWNED, resumed_from_id="p-old")
    registry.store.upsert_participant(child)
    registry.store.create_job(_job("spawn-1", child.id, "spawn", "", 1.0))
    registry.store.create_job(_job("send-1", child.id, "send", "é" * 2000, 2.0))
    registry.store.create_job(_job("send-2", child.id, "send", "again", 3.0))
    calls: list[dict] = []

    async def fake_page(daemon, **kwargs):
        calls.append(kwargs)
        return {"id": kwargs["participant_id"], "events": []}

    monkeypatch.setattr(transcript_read_mod, "read_transcript_page", fake_page)
    daemon = SimpleNamespace(registry=registry)
    read = methods_mod.METHODS["read_transcript"]

    newest = await read(daemon, {"id": child.id, "roles": ["tool_result", "assistant"]})
    older = await read(daemon, {"id": child.id, "cursor": "trc3.x"})

    assert calls[0]["roles"] == ("assistant", "tool_result")
    assert newest["first_prompt"] == "é" * 1024
    assert newest["later_prompts"] == 1
    assert newest["resumed_from"] == "p-old"
    assert older == {"id": child.id, "events": []}
    with pytest.raises(BadRequest, match="assistant"):
        await read(daemon, {"id": child.id, "roles": ["error"]})
