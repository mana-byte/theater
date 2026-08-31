"""Pi's isolated session launch, JSONL observation, and safe resume contract."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from theater.harness.builtin.plugins.pi.constants import PI_ISOLATION_MARKER, PI_RECORD_BYTES
from theater.harness.builtin.plugins.pi.launch import plan_launch, resume_launch_overlay
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext
from theater.harness.contracts.events import EventKind
from theater.models import Participant, Status
from theater.trajectory.enums import TrajectoryKind


def _session(*, session_id: str, cwd: Path) -> dict[str, object]:
    return {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-08-31T12:00:00.000Z",
        "cwd": str(cwd),
    }


def _message(entry_id: str, message: dict[str, object]) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "timestamp": "2026-08-31T12:00:01.000Z",
        "message": message,
    }


def _append(path: Path, *records: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def test_pi_launch_isolated_session_config_and_yolo_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    config_path = tmp_path / "mcp.json"

    plan = plan_launch(
        LaunchContext(
            participant_id="pi-child",
            prompt="inspect this",
            config_path=config_path,
            approval="yolo",
            model="openai/gpt-5.6",
            reasoning_effort="high",
        )
    )

    session_dir = tmp_path / "theater-home" / "observations" / "pi" / "pi-child" / "sessions"
    assert plan.argv == [
        "pi",
        "--session-id",
        "pi-child",
        "--session-dir",
        str(session_dir),
        "--theater-mcp-config",
        str(config_path),
        "--model",
        "openai/gpt-5.6",
        "--thinking",
        "high",
        "inspect this",
    ]
    assert plan.session_id == "pi-child"
    assert plan.transcript_domain == str(session_dir.resolve())
    assert set(plan.files) == {config_path, session_dir / PI_ISOLATION_MARKER}
    config = json.loads(plan.files[config_path])
    assert config["mcpServers"]["theater"]["args"] == [
        "mcp",
        "--id",
        "pi-child",
        "--harness",
        "pi",
    ]


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
    _append(transcript, _session(session_id="native-id", cwd=workdir))
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.session_id == "native-id"
    source.commit_attachment()

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_message("user-1", {"role": "user", "content": "hello"})))
    partial = asyncio.run(source.read())
    assert partial.progressed is True
    assert not partial.events

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    batch = asyncio.run(source.read())
    assert [(event.kind, event.text) for event in batch.events] == [(EventKind.USER, "hello")]


def test_pi_header_identity_does_not_depend_on_the_filename(tmp_path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = tmp_path / "arbitrary-name.jsonl"
    _append(transcript, _session(session_id="native-id", cwd=workdir))

    observer = PiObserver(root=tmp_path)
    assert observer.session_id(transcript) == "native-id"
    assert observer.find_transcript(cwd=str(workdir), session_id="native-id") == (
        transcript.resolve()
    )


def test_pi_parser_pairs_tools_projects_usage_and_ends_the_turn(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    user = observer.parse_record(
        json.dumps(_message("user-1", {"role": "user", "content": "check status"})), 1
    )
    tool_call = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "I should inspect it."},
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {"command": "git status --short"},
                        },
                    ],
                    "stopReason": "toolUse",
                },
            )
        ),
        2,
    )
    tool_result = observer.parse_record(
        json.dumps(
            _message(
                "tool-1",
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "clean"}],
                },
            )
        ),
        3,
    )
    terminal = observer.parse_record(
        json.dumps(
            _message(
                "assistant-2",
                {
                    "role": "assistant",
                    "model": "gpt-5.6",
                    "content": [{"type": "text", "text": "The tree is clean."}],
                    "stopReason": "stop",
                    "usage": {"input": 7, "output": 4, "reasoning": 2, "cost": {"total": 0.01}},
                },
            )
        ),
        4,
    )

    assert user.events[0].turn_id == "user-1"
    assert [event.kind for event in tool_call.events] == [EventKind.TOOL_CALL]
    assert tool_call.trajectory[-1].kind is TrajectoryKind.TOOL_CALL
    assert tool_call.trajectory[-1].call_id == "call-1"
    assert tool_result.events[0].kind is EventKind.TOOL_RESULT
    assert tool_result.trajectory[0].call_id == "call-1"
    assert terminal.events[-1].turn_end is True
    assert terminal.events[-1].usage is not None
    assert terminal.events[-1].usage.input_tokens == 7
    assert any(fact.kind is TrajectoryKind.USAGE for fact in terminal.trajectory)


def test_pi_oversized_record_is_dropped_without_stalling_following_records(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(transcript, _session(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("x" * (PI_RECORD_BYTES + 1) + "\n")
    _append(transcript, _message("user-1", {"role": "user", "content": "survived"}))

    batch = asyncio.run(source.read())
    assert batch.progressed is True
    assert batch.error_code == "pi_transcript_oversized_record"
    assert [(event.kind, event.text) for event in batch.events] == [(EventKind.USER, "survived")]


def test_pi_history_reader_keeps_live_turn_context_and_resume_reuses_session_dir(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    workdir = tmp_path / "work"
    workdir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message("user-1", {"role": "user", "content": "continue"}),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    assert asyncio.run(source.history_page(limit=2)).events

    _append(
        transcript,
        _message(
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
    predecessor = Participant(
        id="predecessor",
        harness="pi",
        cwd=str(workdir),
        session_id="native-id",
        transcript_domain=str(domain),
        transcript_location=str(domain / "native-id.jsonl"),
        session_correlation="exact",
        status=Status.DEAD,
    )
    overlay = resume_launch_overlay(
        ResumeContext(predecessor=predecessor, trusted_session_owners=(predecessor,))
    )
    resumed = plan_launch(
        LaunchContext("successor", "continue", tmp_path / "resume.json", "yolo", resume="native-id")
    )

    assert resumed.session_id == "native-id"
    assert "--session-dir" not in resumed.argv
    assert overlay.env["PI_CODING_AGENT_SESSION_DIR"] == str(domain.resolve())
    assert overlay.transcript_domain == str(domain.resolve())
    assert overlay.cwd == str(workdir)
