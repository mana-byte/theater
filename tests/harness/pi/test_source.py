"""Pi observation: transcript source, direct-bash status, identity and read stamps."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.pi_transcripts import append, bash_execution, message_record, session_record
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.contracts.events import EventKind
from theater.models import Status


def test_pi_source_waits_until_the_initial_session_file_exists(tmp_path) -> None:
    sessions = tmp_path / "sessions"
    workdir = tmp_path / "work"
    workdir.mkdir()
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )

    assert asyncio.run(source.read()).waiting is True

    sessions.mkdir()
    transcript = sessions / "2026-08-31T120000_native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=workdir))
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.session_id == "native-id"
    source.commit_attachment()

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(message_record("user-1", {"role": "user", "content": "hello"})))
    partial = asyncio.run(source.read())
    assert partial.progressed is True
    assert not partial.events

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    batch = asyncio.run(source.read())
    assert [(event.kind, event.text) for event in batch.events] == [(EventKind.USER, "hello")]


def test_pi_source_reports_completed_direct_bash_as_idle(tmp_path) -> None:
    sessions = tmp_path / "sessions"
    workdir = tmp_path / "work"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    asyncio.run(source.read())

    append(transcript, bash_execution())
    batch = asyncio.run(source.read())

    assert batch.status is Status.IDLE
    assert not batch.events
    assert not batch.trajectory


@pytest.mark.parametrize(
    ("records", "expected_status"),
    [
        (
            (
                bash_execution(),
                message_record("user-1", {"role": "user", "content": "continue"}),
            ),
            None,
        ),
        (
            (
                message_record("user-1", {"role": "user", "content": "continue"}),
                bash_execution(),
            ),
            Status.IDLE,
        ),
    ],
)
def test_pi_source_resolves_direct_bash_status_in_record_order(
    tmp_path, records, expected_status
) -> None:
    sessions = tmp_path / "sessions"
    workdir = tmp_path / "work"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    asyncio.run(source.read())

    append(transcript, *records)
    batch = asyncio.run(source.read())

    assert batch.status is expected_status
    assert [event.kind for event in batch.events] == [EventKind.USER]


def test_pi_attachment_restores_completed_direct_bash_status(tmp_path) -> None:
    sessions = tmp_path / "sessions"
    workdir = tmp_path / "work"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        bash_execution(),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )

    attached = asyncio.run(source.read()).attached

    assert attached is not None
    assert attached.last_event is None
    assert attached.status is Status.IDLE


def test_pi_header_identity_does_not_depend_on_the_filename(tmp_path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = tmp_path / "arbitrary-name.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=workdir))

    observer = PiObserver(root=tmp_path)
    assert observer.session_id(transcript) == "native-id"
    assert observer.find_transcript(cwd=str(workdir), session_id="native-id") == (
        transcript.resolve()
    )


def test_pi_adopted_source_attaches_at_eof(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record(
            "assistant-old",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    source = PiObserver(root=sessions).open_source(cwd=str(workdir), session_id="native-id")
    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == 2
    source.commit_attachment()
    assert asyncio.run(source.read()).events == ()


def test_pi_backlog_requests_immediate_repoll(tmp_path, monkeypatch) -> None:
    from theater.harness.builtin.plugins.pi import source as pi_source

    monkeypatch.setattr(pi_source, "PI_RECORDS_PER_BATCH", 1)
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions).open_source(cwd=str(workdir), session_id="native-id")
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    append(
        transcript,
        message_record("user-1", {"role": "user", "content": "one"}),
        message_record("user-2", {"role": "user", "content": "two"}),
    )

    first = asyncio.run(source.read())
    second = asyncio.run(source.read())

    assert first.has_more is True
    assert second.has_more is False


def test_pi_source_stamps_read_at_on_non_empty_reads(tmp_path) -> None:
    sessions, workdir = tmp_path / "sessions", tmp_path / "work"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    append(transcript, session_record(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    asyncio.run(source.read())
    source.commit_attachment()
    asyncio.run(source.read())

    append(transcript, message_record("user-1", {"role": "user", "content": "hi"}))
    batch = asyncio.run(source.read())

    assert batch.events
    assert batch.read_at is not None and batch.read_at > 0
