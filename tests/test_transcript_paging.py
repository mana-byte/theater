from __future__ import annotations

import json
from pathlib import Path

import pytest
from shipped import OpenCodeObserver
from test_harness_opencode import Recorder

from theater.constants.daemon import TRANSCRIPT_READ_RESPONSE_MAX_BYTES
from theater.daemon.rpc.transcript_paging import TranscriptCursorError, TranscriptPager
from theater.harness import Event, EventKind
from theater.harness.contracts.events import TokenUsage
from theater.harness.contracts.source import Batch, HistoryPage, Source
from theater.harness.observation import TranscriptObserver
from theater.harness.transcript.observer import open_participant_source


class StaticSource(Source):
    def __init__(self, pages: dict[str | None, HistoryPage]) -> None:
        self.pages = pages
        self.calls: list[tuple[str | None, str | None, bool]] = []

    async def read(self) -> Batch:
        return Batch()

    async def history_page(
        self,
        *,
        before: str | None = None,
        snapshot: str | None = None,
        limit: int = 200,
        include_full_text: bool = False,
    ) -> HistoryPage:
        self.calls.append((before, snapshot, include_full_text))
        if snapshot is not None:
            return next(
                (page for page in self.pages.values() if page.snapshot_cursor == snapshot),
                self.pages[before],
            )
        return self.pages[before]


def page(
    events: tuple[Event, ...],
    *,
    cursor: str,
    snapshot_cursor: str | None = None,
    older_cursor: str | None = None,
    location: str = "/opaque/transcript",
) -> HistoryPage:
    return HistoryPage(
        location=location,
        events=events,
        complete_events=events,
        cursor=cursor,
        snapshot_cursor=snapshot_cursor or cursor,
        older_cursor=older_cursor,
        has_older=older_cursor is not None,
        provenance="operator",
    )


def pager(source: Source, target: str = "p1") -> TranscriptPager:
    return TranscriptPager(source, target=target, event_filter=lambda _event: True)


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
    recorder = Recorder(tmp_path / "opencode.db", "session", str(workdir))
    try:
        message = recorder.message("message-1", "assistant")
        recorder.text("message-1", "part-1", "é" * 20_000)
        recorder.finish(message, "stop")
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
    recorder = Recorder(tmp_path / "opencode.db", "session", str(workdir))
    try:
        message = recorder.message("message-1", "assistant")
        recorder.text("message-1", "part-1", text)
        recorder.finish(message, "stop")
        source = OpenCodeObserver(db=recorder.path).open_source(
            cwd=str(workdir), session_id="session"
        )
        try:
            first = await pager(source).read()
            newer = recorder.message("message-2", "assistant")
            recorder.text("message-2", "part-2", "newer")
            recorder.finish(newer, "stop")
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
