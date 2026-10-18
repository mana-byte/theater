"""Pi observation: transcript source, direct-bash status, identity, read stamps, screen."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.pi_transcripts import append, bash_execution, message_record, session_record
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.builtin.plugins.pi.screen import classify_screen
from theater.harness.contracts.callbacks import ScreenContext
from theater.harness.contracts.events import EventKind
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind
from theater.models import Status


def test_pi_screen_classifier_is_conservative_and_never_raises() -> None:
    assert classify_screen(ScreenContext(capture="Esc to interrupt")).kind is ScreenKind.UNKNOWN
    assert (
        classify_screen(
            ScreenContext(
                capture=(
                    "escape interrupt · ctrl+c/ctrl+d clear/exit\n"
                    "0.0%/1.0M (auto) (mistral) zai-glm-5-2 • high"
                )
            )
        ).kind
        is ScreenKind.UNKNOWN
    )
    idle = classify_screen(ScreenContext(capture="0.0%/1.0M\nTheater: idle"))
    assert idle.kind is ScreenKind.PROMPT
    assert idle.confidence is ScreenConfidence.HIGH
    # A stale idle marker must never beat a real status indicator.
    assert (
        classify_screen(ScreenContext(capture="⠋ Working...\n0.0%/1.0M\nTheater: idle")).kind
        is ScreenKind.WORKING
    )
    # Extension widgets can displace Pi's loader far above the footer.
    assert (
        classify_screen(
            ScreenContext(capture="⠙ Retrying (1/3)\n" + "widget\n" * 12 + "footer")
        ).kind
        is ScreenKind.WORKING
    )
    # Only the final footer-status line can carry the idle marker.
    assert (
        classify_screen(ScreenContext(capture="Theater: idle\nassistant prose")).kind
        is ScreenKind.UNKNOWN
    )
    prompt = classify_screen(ScreenContext(capture="\n❯"))
    assert prompt.kind is ScreenKind.PROMPT
    assert prompt.confidence is ScreenConfidence.LOW
    assert classify_screen(ScreenContext(capture="agent prose only")).kind is ScreenKind.UNKNOWN
    # The awaiting-input marker beats a spinner frame: a pending question is
    # a button-press pane, the same unrecoverable cost as an approval dialog,
    # so the footer wins over a stale loader frame.
    awaiting = classify_screen(
        ScreenContext(capture="⠙ Working...\n0.0%/1.0M\nTheater: awaiting input")
    )
    assert awaiting.kind is ScreenKind.APPROVAL
    assert awaiting.confidence is ScreenConfidence.HIGH
    # Only the final footer-status line can carry the awaiting marker.
    assert (
        classify_screen(ScreenContext(capture="Theater: awaiting input\nassistant prose")).kind
        is ScreenKind.UNKNOWN
    )
    assert (
        classify_screen(ScreenContext(capture="Theater: awaiting input")).kind
        is ScreenKind.APPROVAL
    )
    # An open overlay — a question dialog, a selector — covers the status
    # footer entirely, so the cancel affordance in the final chrome line is
    # the visible awaiting marker.
    overlay = classify_screen(
        ScreenContext(
            capture=" Proceed with the test?\n"
            "❯ 1. Yes\n"
            " Enter to select · ↑/↓ to navigate · Esc to cancel"
        )
    )
    assert overlay.kind is ScreenKind.APPROVAL
    assert overlay.confidence is ScreenConfidence.HIGH
    # The affordance only counts as the final chrome line: prose that
    # mentions it mid-screen cannot spoof an awaiting reading.
    assert (
        classify_screen(
            ScreenContext(capture="assistant mentions Esc to cancel\n0.0%/1.0M\nTheater: idle")
        ).kind
        is ScreenKind.PROMPT
    )


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
