"""Pi oversized records and history paging keep semantics and stream positions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.support.pi_transcripts import append, message_record, session_record
from theater.harness.builtin.plugins.pi.constants import (
    PI_ISOLATION_MARKER,
)
from theater.harness.builtin.plugins.pi.launch import (
    participant_root,
    plan_launch,
    resume_launch_overlay,
)
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.models import Participant, Status
from theater.trajectory.enums import (
    TrajectoryKind,
    TrajectoryStatus,
)


def test_pi_oversized_record_preserves_following_indices_and_checkpoint(
    tmp_path, monkeypatch
) -> None:
    from theater.harness.builtin.plugins.pi import source as pi_source

    monkeypatch.setattr(pi_source, "PI_RECORD_BYTES", 1024)
    monkeypatch.setattr(pi_source, "PI_READ_BYTES", 256)
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()

    append(transcript, message_record("user-before", {"role": "user", "content": "before"}))
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("x" * 1025 + "\n")
    append(transcript, message_record("user-1", {"role": "user", "content": "survived"}))

    batches = []
    for _ in range(20):
        batch = asyncio.run(source.read())
        batches.append(batch)
        source.acknowledge_source_checkpoint()
        if not batch.has_more:
            break
    assert not batches[-1].has_more
    assert [batch.error_code for batch in batches if batch.error_code] == [
        "pi_transcript_oversized_record"
    ]
    assert [(event.text, event.raw_index) for batch in batches for event in batch.events] == [
        ("before", 1),
        ("survived", 3),
    ]
    checkpoint = json.loads(source.source_checkpoint())
    assert (checkpoint["offset"], checkpoint["records"]) == (transcript.stat().st_size, 4)


def test_pi_truncation_stages_a_batch_checkpoint_without_an_attachment(tmp_path) -> None:
    transcript = tmp_path / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=tmp_path))
    source = PiObserver(root=tmp_path, isolated=True).open_source(
        cwd=str(tmp_path), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
    original = source.source_checkpoint()
    append(transcript, message_record("user", {"role": "user", "content": "read"}))
    assert asyncio.run(source.read()).events
    assert json.loads(source.pending_source_checkpoint())["offset"] == transcript.stat().st_size

    transcript.write_bytes(b"")
    assert asyncio.run(source.read()).attached is None
    reset = source.pending_source_checkpoint()
    assert reset is not None
    assert (json.loads(reset)["offset"], json.loads(reset)["records"]) == (0, 0)
    assert source.source_checkpoint() == original
    source.acknowledge_source_checkpoint()
    assert source.source_checkpoint() == reset


def test_pi_rollback_keeps_a_same_inode_byte_floor_without_recounting_records(tmp_path) -> None:
    transcript = tmp_path / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=tmp_path))
    source = PiObserver(root=tmp_path, isolated=True).open_source(
        cwd=str(tmp_path), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    append(transcript, message_record("user", {"role": "user", "content": "read"}))
    assert asyncio.run(source.read()).events
    source.acknowledge_source_checkpoint()
    offset, index = source.offset, source.index

    original = transcript.read_bytes()
    transcript.write_bytes(original.replace(b"\n", b" ", 1))
    assert transcript.stat().st_size == offset
    assert transcript.read_bytes().count(b"\n") < index
    source.rollback_source_checkpoint()
    assert (source.path, source.offset, source.index) == (transcript, offset, index)


@pytest.mark.asyncio
async def test_pi_cancelled_projection_cannot_advance_live_context(tmp_path, monkeypatch) -> None:
    import threading

    from theater.harness.builtin.plugins.pi import source as pi_source

    transcript = tmp_path / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=tmp_path))
    source = PiObserver(root=tmp_path, isolated=True).open_source(
        cwd=str(tmp_path), session_id="native-id"
    )
    assert (await source.read()).attached is not None
    source.commit_attachment()
    append(transcript, message_record("user", {"role": "user", "content": "hello"}))
    initial = source._observer._snapshot_turn_context()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    parse = pi_source.parse_live_records

    def delayed(*args):
        started.set()
        assert release.wait(10)
        try:
            return parse(*args)
        finally:
            finished.set()

    monkeypatch.setattr(pi_source, "parse_live_records", delayed)
    task = asyncio.create_task(source.read())
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    assert source._observer._snapshot_turn_context() == initial
    assert source.index == 0
    assert [(event.text, event.raw_index) for event in (await source.read()).events] == [
        ("hello", 1)
    ]


@pytest.mark.asyncio
async def test_pi_large_records_retain_semantics_and_original_stream_positions(
    tmp_path, monkeypatch
) -> None:
    from theater.harness.builtin.plugins.pi import source as pi_source
    from theater.harness.builtin.plugins.pi.constants import PI_PROJECTED_RECORD_BYTES

    monkeypatch.setattr(pi_source, "PI_RECORDS_PER_BATCH", 2)
    transcript = tmp_path / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=tmp_path))
    source = PiObserver(root=tmp_path, isolated=True).open_source(
        cwd=str(tmp_path), session_id="native-id"
    )
    assert (await source.read()).attached is not None
    source.commit_attachment()
    append(
        transcript,
        message_record("user", {"role": "user", "content": "read the image"}),
        message_record(
            "call",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "", "textSignature": "x" * 80}] * 1220
                + [
                    {
                        "type": "toolCall",
                        "id": "image-call",
                        "name": "read",
                        "arguments": {"path": "image.png", "limit": 1},
                    }
                ],
                "stopReason": "toolUse",
                "usage": {"input": 123, "output": 4},
            },
        ),
        message_record(
            "result",
            {
                "role": "toolResult",
                "toolCallId": "image-call",
                "toolName": "read",
                "isError": False,
                "content": [{"type": "image", "mimeType": "image/png", "data": "a" * 309339}],
            },
        ),
        message_record(
            "answer",
            {
                "role": "assistant",
                "content": "résultat " * 12000,
                "stopReason": "stop",
                "usage": {"input": 456, "output": 7},
            },
        ),
    )
    boundaries = []
    with transcript.open("rb") as stream:
        for raw in stream:
            boundaries.append(len(raw))
    batches = []
    for _ in range(20):
        batch = await source.read()
        batches.append(batch)
        source.acknowledge_source_checkpoint()
        if not batch.has_more:
            break
    assert not batches[-1].has_more
    assert len(batches) >= 2
    assert not any(batch.error_code for batch in batches)
    events = [event for batch in batches for event in batch.events]
    facts = [fact for batch in batches for fact in batch.trajectory]
    assert [event.usage.input_tokens for event in events if event.usage] == [123, 456]
    assert [
        (fact.call_id, fact.status) for fact in facts if fact.kind is TrajectoryKind.TOOL_RESULT
    ] == [("image-call", TrajectoryStatus.COMPLETED)]
    call = next(fact for fact in facts if fact.kind is TrajectoryKind.TOOL_CALL)
    arguments = next(detail for detail in call.details if detail.name == "arguments")
    assert arguments.value.text == json.dumps({"limit": 1, "path": "image.png"})
    assert [(event.raw_index, event.turn_id) for event in events if event.turn_end] == [(4, "user")]
    assert all(event.source_offset == sum(boundaries[: event.raw_index]) for event in events)
    assert all(
        len((event.raw_text or "").encode()) <= PI_PROJECTED_RECORD_BYTES for event in events
    )
    assert "bytes omitted" in next(
        event.raw_text for event in events if event.raw_index == 4 and event.raw_text
    )
    assert "a" * 100 not in repr(facts)
    checkpoint = source.source_checkpoint()
    assert json.loads(checkpoint)["offset"] == transcript.stat().st_size
    assert json.loads(checkpoint)["records"] == 5
    source.rollback_source_checkpoint()
    assert not (await source.read()).events

    restarted = PiObserver(root=tmp_path, isolated=True).open_source(
        cwd=str(tmp_path),
        session_id="native-id",
        known_location=str(transcript),
        source_checkpoint=checkpoint,
    )
    assert (await restarted.read()).attached is not None
    restarted.commit_attachment()
    assert not (await restarted.read()).events


def test_pi_history_seed_io_error_is_source_unavailable(tmp_path, monkeypatch) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record("user-1", {"role": "user", "content": "continue"}),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )

    def unreadable_seed(self, stream, start):
        raise OSError("seed read failed")

    monkeypatch.setattr(PiObserver, "_seed_history_context", unreadable_seed)
    failed = asyncio.run(source.history_page(limit=2))
    assert failed.error_code == "transcript_source_unavailable"


@pytest.mark.usefixtures("available_harness_binaries")
def test_pi_history_reader_keeps_live_turn_context_and_resume_forks_into_new_session_dir(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    workdir = tmp_path / "work"
    workdir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record("user-1", {"role": "user", "content": "continue"}),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    assert asyncio.run(source.history_page(limit=2)).events

    append(
        transcript,
        message_record(
            "assistant-1",
            {"role": "assistant", "content": "done", "stopReason": "stop"},
        ),
    )
    live = asyncio.run(source.read())
    assert live.events[0].turn_id == "user-1"

    cold_plan = plan_launch(LaunchContext("predecessor", "", tmp_path / "config.json", "yolo"))
    domain = Path(cold_plan.transcript_domain or "")
    domain.mkdir(parents=True)
    marker = domain / PI_ISOLATION_MARKER
    marker.write_text(cold_plan.files[marker], encoding="utf-8")
    predecessor_transcript = domain / "native-id.jsonl"
    append(predecessor_transcript, session_record(session_id="native-id", cwd=workdir))
    predecessor = Participant(
        id="predecessor",
        harness="pi",
        cwd=str(workdir),
        session_id="native-id",
        transcript_domain=str(domain),
        transcript_location=str(predecessor_transcript),
        session_correlation="exact",
        status=Status.DEAD,
    )
    overlay = resume_launch_overlay(
        ResumeContext(predecessor=predecessor, trusted_session_owners=(predecessor,))
    )
    resumed = plan_launch(
        LaunchContext(
            "successor",
            "continue",
            tmp_path / "resume.json",
            "yolo",
            resume=overlay.resume_reference,
        )
    )

    successor_domain = participant_root("successor")
    assert resumed.session_id == "successor"
    assert resumed.argv[resumed.argv.index("--session-dir") + 1] == str(successor_domain)
    assert resumed.argv[resumed.argv.index("--fork") + 1] == str(predecessor_transcript.resolve())
    assert resumed.transcript_domain == str(successor_domain.resolve())
    assert successor_domain / PI_ISOLATION_MARKER in resumed.files
    assert overlay.resume_reference == str(predecessor_transcript.resolve())
    assert overlay.transcript_domain is None
    assert overlay.cwd == str(workdir)
