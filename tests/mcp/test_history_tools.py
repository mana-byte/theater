"""The read_transcript, recall and recall_read tool bodies."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from tests.support.claude_seal import claude_fragment, claude_marker, claude_record
from tests.support.mcp_tools import resolved
from tests.support.transcript_pages import StaticSource, page, pager
from theater.daemon.rpc.transcripts import _read_transcript
from theater.daemon.transcript_paging import TranscriptEventChunk, TranscriptReadPage
from theater.harness import HARNESSES, Event, EventKind
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.mcp import tools
from theater.mcp.toolsets.transcripts import read_transcript


async def test_read_transcript_forwards_a_live_name_without_listing_first():
    s = resolved(read_transcript={"id": "p-you", "events": []})
    await tools.read_transcript(s, target="Arlequin")
    assert s.client.methods == ["read_transcript"]
    assert s.client.params("read_transcript") == {"id": "Arlequin"}


async def test_read_transcript_forwards_only_the_returned_cursor():
    s = resolved(
        read_transcript={
            "id": "p-you",
            "events": [],
            "cursor": "trc1.cursor",
            "next_cursor": None,
            "has_more": False,
        }
    )
    page = await tools.read_transcript(
        s, target="Arlequin", cursor="trc1.cursor", roles=["assistant"]
    )
    assert s.client.params("read_transcript") == {
        "id": "Arlequin",
        "cursor": "trc1.cursor",
        "roles": ["assistant"],
    }
    assert page == {"id": "p-you", "events": [], "next_cursor": None}


async def test_only_complete_causally_matching_answer_and_seal_are_coalesced(daemon, monkeypatch):
    observer = ClaudeCodeObserver()
    lines = [
        claude_record("u0", None, "user", message={"content": "ask"}),
        claude_fragment("u1", "u0", {"type": "text", "text": "answer"}, message="other"),
        claude_fragment("u2", "u1", {"type": "text", "text": "answer"}),
        claude_marker("u3", "u2"),
    ]
    events = tuple(event for i, line in enumerate(lines) for event in observer.parse(line, i))
    history = page(events, cursor="snapshot", older_cursor="older")
    source = StaticSource({None: history})
    monkeypatch.setitem(
        HARNESSES,
        "fixture",
        SimpleNamespace(observer=SimpleNamespace(open_source=lambda **_: source)),
    )
    participant = daemon.registry.register(harness="fixture", pane=None, cwd="/tmp/seals")
    original = await pager(source, target=participant.id).read()
    wire = await _read_transcript(daemon, {"id": participant.id})
    assert original.answer_seals() == {3: 2}
    assert [event.turn_end for event in events] == [False, False, False, True]

    compact = await read_transcript(resolved(read_transcript=wire), target="p1")

    assert compact["events"] == [
        {"role": "user", "text": "ask"},
        {"role": "assistant", "text": "answer"},
        {"role": "assistant", "text": "answer", "turn_end": True},
    ]
    assert compact["next_cursor"] == original.next_cursor
    assert compact["truncated"] == original.truncated
    assert len(wire["events"]) == 4
    assert "repeats_event_position" not in original.to_wire(target="p1")["events"][-1]
    partial = replace(original.events[2], start=1)
    assert (
        replace(original, events=(*original.events[:2], partial, original.events[3])).answer_seals()
        == {}
    )
    anonymous = replace(original.events[2], event=replace(events[2], turn_id=None))
    assert (
        replace(
            original, events=(*original.events[:2], anonymous, original.events[3])
        ).answer_seals()
        == {}
    )


async def test_sparse_events_preserve_chunks_tools_terminal_evidence_and_prompt_context():
    events = (
        Event(EventKind.USER, text="ask", raw_index=0),
        Event(EventKind.TOOL_CALL, tool_name="Read", raw_index=1),
        Event(EventKind.TOOL_RESULT, tool_name="Read", raw_index=2),
        Event(EventKind.ASSISTANT, text="éanswer", raw_index=3),
    )
    chunks = tuple(
        TranscriptEventChunk(
            event, i, event.text, 0, len(event.text.encode()), len(event.text.encode())
        )
        for i, event in enumerate(events)
    )
    partial = replace(chunks[-1], text="answer", start=2)
    collapsed = replace(chunks[-1], text="é", tail="r", elided_bytes=5, middle_cursor="middle")
    original = TranscriptReadPage(
        page(events, cursor="snapshot"), None, (*chunks[:3], partial, collapsed), "older", True
    )
    wire = original.to_wire(target="p1")
    wire.update(first_prompt="ask", later_prompts=0, resumed_from="p-old")
    wire["events"][2].update(turn_end=True, turn_terminal="completed")
    compact = await read_transcript(resolved(read_transcript=wire), target="p1")

    assert compact["events"][:3] == [
        {"role": "user", "text": "ask"},
        {"role": "tool_call", "tool_name": "Read"},
        {
            "role": "tool_result",
            "tool_name": "Read",
            "turn_end": True,
            "turn_terminal": "completed",
        },
    ]
    assert compact["events"][3] == {
        "role": "assistant",
        "text": "answer",
        "index": 3,
        "text_start_byte": 2,
        "text_end_byte": 8,
        "text_total_bytes": 8,
    }
    assert compact["events"][4] == {
        "role": "assistant",
        "text": "é",
        "index": 3,
        "text_start_byte": 0,
        "text_end_byte": 8,
        "text_total_bytes": 8,
        "tail": "r",
        "elided_bytes": 5,
        "middle_cursor": "middle",
    }
    assert "first_prompt" not in compact
    assert compact["later_prompts"] == 0
    assert compact["resumed_from"] == "p-old"
    filtered = await read_transcript(
        resolved(read_transcript=wire), target="p1", roles=["assistant"]
    )
    assert not {"first_prompt", "later_prompts", "resumed_from"} & filtered.keys()
    wire["first_prompt"] = "not on this page"
    assert (await read_transcript(resolved(read_transcript=wire), target="p1"))[
        "first_prompt"
    ] == "not on this page"


async def test_recall_passes_paths_and_depth_to_the_daemon():
    """The daemon does the query; the tool body just forwards the args."""
    s = resolved(recall={"src/main.py": {"timeline": []}})
    await tools.recall(s, paths=["src/main.py"], depth=3)
    p = s.client.params("recall")
    assert p["paths"] == ["src/main.py"]
    assert p["depth"] == 3
    assert p["caller_cwd"]  # the tool sends its own cwd


async def test_recall_omits_handle_when_segment_already_identifies_the_point():
    point = {
        "handle": "p-you#4",
        "segment": "p-you#4",
        "outcome": "done",
        "participant_id": "p-you",
        "name": "Builder",
    }
    s = resolved(recall={"x.py": {"timeline": [point]}})

    result = await tools.recall(s, paths=["x.py"])

    assert result["x.py"]["timeline"] == [
        {
            "segment": "p-you#4",
            "outcome": "done",
            "participant_id": "p-you",
            "name": "Builder",
        }
    ]


async def test_recall_read_forwards_the_segment_id_verbatim():
    """The segment id is a contract between recall and recall_read.

    A gap id is ``gap:<path>:<before>..<after>``, so it carries colons
    and dots that any normalising on the way through would corrupt.
    """
    seg = "gap:theater/daemon/store.py:2a0ff31..e91c4d2"
    s = resolved(recall_read={"kind": "gap"})
    await tools.recall_read(s, segment_id=seg)
    p = s.client.params("recall_read")
    assert p["segment_id"] == seg
    assert p["caller_cwd"]  # the git root for the privacy wall
