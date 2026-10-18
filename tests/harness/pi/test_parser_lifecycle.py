"""Pi parser turn lifecycle: tool pairing, settled markers, retries and partial calls."""

from __future__ import annotations

import json

import pytest

from tests.support.pi_transcripts import append, bash_execution, message_record, session_record
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.contracts.events import EventKind, TurnTerminal
from theater.models import Status
from theater.trajectory.enums import (
    TrajectoryKind,
    TrajectoryStatus,
)
from theater.trajectory.tools import tool_operations_for_records


def _lifecycle(phase: str, entry_id: str = "life-1") -> dict[str, object]:
    return {
        "type": "custom",
        "id": entry_id,
        "timestamp": "2026-08-31T12:00:02.000Z",
        "customType": "theater:lifecycle",
        "data": {"version": 1, "phase": phase},
    }


def test_pi_parser_pairs_tools_projects_usage_and_ends_the_turn(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    user = observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "check status"})), 1
    )
    tool_call = observer.parse_record(
        json.dumps(
            message_record(
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
            message_record(
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
            message_record(
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
    assert user.trajectory[0].status is TrajectoryStatus.COMPLETED
    assert [event.kind for event in tool_call.events] == [EventKind.TOOL_CALL]
    assert tool_call.trajectory[-1].kind is TrajectoryKind.TOOL_CALL
    assert tool_call.trajectory[-1].call_id == "call-1"
    assistant = next(fact for fact in tool_call.trajectory if fact.kind is TrajectoryKind.ASSISTANT)
    assert assistant.status is TrajectoryStatus.COMPLETED
    assert tool_call.trajectory[-1].status is TrajectoryStatus.PENDING
    assert tool_result.events[0].kind is EventKind.TOOL_RESULT
    assert tool_result.trajectory[0].call_id == "call-1"
    assert tool_result.trajectory[0].status is TrajectoryStatus.COMPLETED
    tool_records = tuple(
        fact_to_record(fact, participant_id="pi-child", source_epoch="epoch")
        for parsed in (tool_call, tool_result)
        for fact in parsed.trajectory
        if fact.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
    )
    assert tool_operations_for_records(tool_records)[0].status is TrajectoryStatus.COMPLETED
    assert terminal.events[-1].turn_end is True
    assert terminal.events[-1].turn_terminal is TurnTerminal.COMPLETED
    assert terminal.events[-1].usage is not None
    assert terminal.events[-1].usage.input_tokens == 7
    assert all(fact.status is TrajectoryStatus.COMPLETED for fact in terminal.trajectory)


@pytest.mark.parametrize(
    ("exit_code", "cancelled"),
    [(0, False), (1, False), (None, True)],
)
def test_pi_parser_treats_completed_direct_bash_as_idle_without_a_turn(
    tmp_path, exit_code, cancelled
) -> None:
    parsed = PiObserver(root=tmp_path).parse_record(
        json.dumps(bash_execution(exit_code=exit_code, cancelled=cancelled)), 1
    )

    assert parsed.status is Status.IDLE
    assert parsed.events == ()
    assert parsed.trajectory == ()


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("error", TrajectoryStatus.ERROR),
        ("aborted", TrajectoryStatus.INTERRUPTED),
        (None, TrajectoryStatus.UNKNOWN),
    ],
)
def test_pi_parser_preserves_non_success_assistant_statuses(
    tmp_path, stop_reason, expected
) -> None:
    message: dict[str, object] = {
        "role": "assistant",
        "content": [{"type": "text", "text": "partial"}],
    }
    if stop_reason is not None:
        message["stopReason"] = stop_reason

    parsed = PiObserver(root=tmp_path).parse_record(
        json.dumps(message_record("assistant-1", message)), 1
    )

    assert parsed.trajectory[0].status is expected


def test_pi_parser_error_response_defers_turn_end_until_settled_marker(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    user = observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    error = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    # The error must not close the turn yet (no turn_end), though it still
    # emits its error content/trajectory facts.
    assert not any(event.turn_end for event in error.events)
    assert [event.kind for event in error.events] == [EventKind.ERROR]
    assert error.trajectory[0].status is TrajectoryStatus.ERROR
    assert observer._pending_terminal_turn_id == "user-1"
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 3)
    assert [event.kind for event in settled.events] == [EventKind.ASSISTANT]
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_terminal is TurnTerminal.FAILED
    assert settled.events[0].turn_id == "user-1"
    # No duplicate assistant text, usage, or trajectory facts on the marker.
    assert settled.events[0].text == ""
    assert settled.events[0].usage is None
    assert settled.trajectory == ()
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    assert observer._active_turn_id is None
    _ = user


def test_pi_parser_error_then_retry_marker_has_no_turn_end(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    error = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    assert not any(event.turn_end for event in error.events)
    assert observer._pending_terminal_turn_id == "user-1"
    # A retry-scheduled marker is an informational no-op: it signals continued
    # work, not its success. Compaction may still fail and then settled must
    # close, so the pending terminal is retained.
    retry = observer.parse_record(json.dumps(_lifecycle("retry-scheduled")), 3)
    assert not retry.events
    assert observer._pending_terminal is True
    assert observer._pending_terminal_turn_id == "user-1"
    assert observer._active_turn_id == "user-1"


def test_pi_parser_settled_marker_may_arrive_in_a_later_parse_record_call(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    # Simulate drain batches arriving later: a tool result then the marker.
    tool_result = observer.parse_record(
        json.dumps(
            message_record(
                "tool-1",
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "ok"}],
                },
            )
        ),
        3,
    )
    assert tool_result.events[0].kind is EventKind.TOOL_RESULT
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 4)
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_id == "user-1"
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None


def test_pi_parser_length_plus_compaction_marker_stays_open(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "long task"})), 1
    )
    length = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial..."}],
                    "stopReason": "length",
                },
            )
        ),
        2,
    )
    # length is recoverable (compact-and-retry); must not close.
    assert not any(event.turn_end for event in length.events)
    assert observer._pending_terminal_turn_id == "user-1"
    assert observer._pending_terminal is True
    compaction = observer.parse_record(json.dumps(_lifecycle("compaction-will-retry")), 3)
    assert not compaction.events
    # compaction-will-retry is informational: compaction may still fail, so the
    # pending terminal is retained until settled.
    assert observer._pending_terminal is True
    assert observer._pending_terminal_turn_id == "user-1"
    assert observer._active_turn_id == "user-1"


def test_pi_parser_retry_success_closes_once_and_clears_pending(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    assert observer._pending_terminal_turn_id == "user-1"
    # A later successful stop closes normally and clears the stale pending state.
    success = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-2",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "stopReason": "stop",
                },
            )
        ),
        3,
    )
    assert success.events[-1].turn_end is True
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    assert observer._active_turn_id is None
    # A subsequent settled marker with no pending terminal is a no-op.
    redundant = observer.parse_record(json.dumps(_lifecycle("settled")), 4)
    assert not redundant.events


def test_pi_parser_malformed_lifecycle_marker_ignored(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    for malformed in (
        {
            "type": "custom",
            "customType": "theater:lifecycle",
            "data": {"version": 2, "phase": "settled"},
        },
        {
            "type": "custom",
            "customType": "theater:lifecycle",
            "data": {"version": 1, "phase": "unknown"},
        },
        {"type": "custom", "customType": "other", "data": {"version": 1, "phase": "settled"}},
        {"type": "custom", "customType": "theater:lifecycle", "data": "not-a-dict"},
        {"type": "custom", "customType": "theater:lifecycle"},
        {
            "type": "message",
            "customType": "theater:lifecycle",
            "data": {"version": 1, "phase": "settled"},
        },
    ):
        parsed = observer.parse_record(json.dumps(malformed), 3)
        assert not parsed.events
        assert parsed.trajectory == ()
    # The pending terminal is untouched by malformed markers.
    assert observer._pending_terminal_turn_id == "user-1"


def test_pi_parser_aborted_partial_calls_are_interrupted_not_pending(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    parsed = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {},
                        }
                    ],
                    "stopReason": "aborted",
                    "errorMessage": "user cancelled",
                },
            )
        ),
        2,
    )
    # Aborted defers the turn_end (a settled marker will release it).
    assert not any(event.turn_end for event in parsed.events)
    call = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.TOOL_CALL)
    assert call.status is TrajectoryStatus.INTERRUPTED
    assert observer._pending_terminal_turn_id == "user-1"
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 3)
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_terminal is TurnTerminal.INTERRUPTED


def test_pi_parser_error_partial_calls_are_error_not_pending(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    parsed = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {},
                        }
                    ],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    assert not any(event.turn_end for event in parsed.events)
    call = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.TOOL_CALL)
    assert call.status is TrajectoryStatus.ERROR


def test_pi_parser_length_with_calls_marks_calls_error(tmp_path) -> None:
    # A recoverable length response is discarded by Pi's agent-core and retried;
    # its toolCall blocks never execute, so they must be ERROR, not PENDING.
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    parsed = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "partial"},
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {},
                        },
                    ],
                    "stopReason": "length",
                },
            )
        ),
        2,
    )
    assert not any(event.turn_end for event in parsed.events)
    call = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.TOOL_CALL)
    assert call.status is TrajectoryStatus.ERROR
    assert observer._pending_terminal_turn_id == "user-1"


def test_pi_parser_reset_turn_context_clears_lifecycle_state(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "do it"})), 1
    )
    observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    assert observer._pending_terminal is True
    assert observer._pending_terminal_turn_id == "user-1"
    observer._reset_turn_context()
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    assert observer._active_turn_id is None


def test_pi_parser_history_seeding_reconstructs_lifecycle_state(tmp_path) -> None:
    import io

    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = tmp_path / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record("user-1", {"role": "user", "content": "do it"}),
        message_record(
            "assistant-1",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
                "stopReason": "error",
                "errorMessage": "overloaded",
            },
        ),
        _lifecycle("settled"),
        message_record("user-2", {"role": "user", "content": "next"}),
    )
    observer = PiObserver(root=tmp_path)
    # Seeding from just before the second user message replays the settled marker
    # and the error, leaving no stale pending terminal across the gap.
    offset = transcript.stat().st_size
    stream = io.BufferedReader(io.BytesIO(transcript.read_bytes()))
    observer._seed_history_context(stream, offset)
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    # The last user message reopens the active turn.
    assert observer._active_turn_id == "user-2"


def test_pi_parser_history_seeding_deferred_without_settled_keeps_pending(tmp_path) -> None:
    import io

    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = tmp_path / "native-id.jsonl"
    append(
        transcript,
        session_record(session_id="native-id", cwd=workdir),
        message_record("user-1", {"role": "user", "content": "do it"}),
        message_record(
            "assistant-1",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
                "stopReason": "error",
                "errorMessage": "overloaded",
            },
        ),
    )
    observer = PiObserver(root=tmp_path)
    offset = transcript.stat().st_size
    stream = io.BufferedReader(io.BytesIO(transcript.read_bytes()))
    observer._seed_history_context(stream, offset)
    # No settled marker arrived in history: the deferred terminal is retained.
    assert observer._pending_terminal is True
    assert observer._pending_terminal_turn_id == "user-1"


def test_pi_parser_compaction_will_retry_then_settled_closes_without_later_assistant(
    tmp_path,
) -> None:
    # Compaction may fail and then agent_settled fires with no later assistant
    # response. The pending terminal from the length response must still close.
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(message_record("user-1", {"role": "user", "content": "long task"})), 1
    )
    length = observer.parse_record(
        json.dumps(
            message_record(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial..."}],
                    "stopReason": "length",
                },
            )
        ),
        2,
    )
    assert observer._pending_terminal is True
    compaction = observer.parse_record(json.dumps(_lifecycle("compaction-will-retry")), 3)
    assert not compaction.events
    # The informational compaction marker did NOT clear the pending terminal.
    assert observer._pending_terminal is True
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 4)
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_id == "user-1"
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    _ = length


def test_pi_parser_anonymous_assistant_record_settled_closes_with_turn_id_none(tmp_path) -> None:
    # An assistant record with no entry id still defers; the pending candidate
    # is tracked by presence, not by the (possibly None) turn id, so settled
    # still releases one synthetic turn_end. The turn id is the active turn's,
    # which may itself be None when there was no preceding user record.
    observer = PiObserver(root=tmp_path)
    record = {
        "type": "message",
        "timestamp": "2026-08-31T12:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
            "stopReason": "error",
            "errorMessage": "overloaded",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assert not any(event.turn_end for event in parsed.events)
    assert observer._pending_terminal is True
    # No preceding user record => active turn id is None, but pending is True.
    assert observer._pending_terminal_turn_id is None
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 2)
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_id is None
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    _ = parsed
