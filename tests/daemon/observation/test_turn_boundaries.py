"""A drained batch is not a turn: each turn end answers its own job, in order."""

from __future__ import annotations

import json
import time

import pytest

from tests.support.observation import poised
from tests.support.observer_rigs import said
from tests.support.pi_transcripts import message_record
from theater.daemon.observer import (
    Turn,
    TurnAccumulator,
)
from theater.harness.base import Event, EventKind, TurnTerminal
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.source import Batch


def spoke(text: str) -> Event:
    return Event(kind=EventKind.USER, text=text, turn_end=False)


def test_turn_positional_constructor_keeps_heard_position():
    turn = Turn("answer", ("prompt",))
    assert turn.said == "answer"
    assert turn.heard == ("prompt",)
    assert turn.raw_said == ""


def test_a_turn_end_mid_batch_still_answers(registry):
    """The reply plus the next prompt arrive together. The reply still lands."""
    observer, _screen, clock, p, jobs = poised(registry)
    batch = Batch(events=[said("the answer", turn_end=True), spoke("and now this")])
    observer._apply(p.id, batch, clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == "the answer"


@pytest.mark.parametrize(
    ("terminal", "expected_state", "expected_error_code"),
    [
        (None, "done", None),
        (TurnTerminal.COMPLETED, "done", None),
        (TurnTerminal.INTERRUPTED, "killed", "interrupted"),
        (TurnTerminal.FAILED, "crashed", "turn_failed"),
    ],
)
def test_the_boundary_outcome_decides_how_the_answered_job_finishes(
    registry, terminal, expected_state, expected_error_code
):
    """Same reply text, different boundary evidence: only the outcome differs."""
    observer, _screen, clock, p, jobs = poised(registry)
    event = Event(
        kind=EventKind.ASSISTANT,
        text="the answer",
        turn_end=True,
        turn_terminal=terminal,
    )
    observer._apply(p.id, Batch(events=[event]), clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == expected_state
    assert job.error_code == expected_error_code
    assert job.result == "the answer"
    if terminal is TurnTerminal.FAILED:
        from theater.daemon.rpc.jobs import _job_to_dict

        assert "turn failed" in _job_to_dict(job)["error"]


@pytest.mark.parametrize(
    ("terminal", "expected_state", "expected_error_code"),
    [
        (TurnTerminal.COMPLETED, "done", None),
        (TurnTerminal.INTERRUPTED, "killed", "interrupted"),
        (TurnTerminal.FAILED, "crashed", "turn_failed"),
    ],
)
def test_the_attach_time_outcome_decides_how_the_answered_job_finishes(
    registry, terminal, expected_state, expected_error_code
):
    """Settling from an attach-time boundary event carries the same outcomes."""
    observer, _screen, _clock, p, jobs = poised(registry)
    event = Event(
        kind=EventKind.ASSISTANT,
        text="the answer",
        turn_end=True,
        turn_terminal=terminal,
    )
    observer._settle_from_event(p.id, event, registration=None)

    job = jobs.get("h1")
    assert str(job.state) == expected_state
    assert job.error_code == expected_error_code
    assert job.result == "the answer"


def test_multi_block_raw_assistant_text_becomes_structured_result(registry):
    observer, _screen, clock, p, jobs = poised(registry, response_format="json")
    batch = Batch(
        events=[
            Event(
                kind=EventKind.ASSISTANT,
                text='{"answer":',
                raw_text='{"answer":',
            ),
            Event(
                kind=EventKind.ASSISTANT,
                text='"clipped"}',
                raw_text='"raw"}',
            ),
            Event(kind=EventKind.ASSISTANT, turn_end=True),
        ]
    )
    observer._apply(p.id, batch, clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == '{"answer":\n\n"clipped"}'
    assert job.structured_result == '{"answer":\n\n"raw"}'
    assert job.structured_status == "parsed"


@pytest.mark.parametrize("final", ['{"answer":"final"}', ""])
def test_marked_final_answer_replaces_earlier_narration_even_when_empty(registry, final):
    observer, _screen, clock, p, jobs = poised(registry, response_format="json")
    observer._apply(
        p.id,
        Batch(
            events=[
                Event(kind=EventKind.ASSISTANT, text="Working on it.", turn_id="turn-1"),
                Event(
                    kind=EventKind.ASSISTANT,
                    text=final,
                    raw_text=final,
                    turn_id="turn-1",
                    turn_end=True,
                    answer_final=True,
                ),
            ]
        ),
        clock,
        TurnAccumulator(),
    )
    job = jobs.get("h1")
    assert job.result == final
    assert job.structured_status == ("parsed" if final else "unavailable")


def test_unmarked_empty_boundary_retains_accumulated_text(registry):
    observer, _screen, clock, p, jobs = poised(registry, response_format="json")
    observer._apply(
        p.id,
        Batch(
            events=[
                Event(kind=EventKind.ASSISTANT, text='{"answer":"reported"}', turn_id="msg-1"),
                Event(
                    kind=EventKind.ASSISTANT,
                    text="",
                    raw_text="",
                    turn_id="msg-1",
                    turn_end=True,
                ),
            ]
        ),
        clock,
        TurnAccumulator(),
    )
    assert jobs.get("h1").structured_result == '{"answer":"reported"}'


def test_pi_final_text_and_usage_settle_together(registry, tmp_path):
    observer, _screen, clock, p, jobs = poised(registry, response_format="json")
    pi = PiObserver(root=tmp_path)
    pi.parse_record(json.dumps(message_record("u1", {"role": "user", "content": "work"})), 1)
    narration = pi.parse_record(
        json.dumps(
            message_record(
                "a1", {"role": "assistant", "content": "Working.", "stopReason": "toolUse"}
            )
        ),
        2,
    )
    terminal = pi.parse_record(
        json.dumps(
            message_record(
                "a2",
                {
                    "role": "assistant",
                    "content": '{"answer":"done"}',
                    "stopReason": "stop",
                    "usage": {"input": 7, "output": 4},
                },
            )
        ),
        3,
    )
    observer._apply(
        p.id,
        Batch(events=[*narration.events, *terminal.events]),
        clock,
        TurnAccumulator(),
    )
    job = jobs.get("h1")
    assert job.result == '{"answer":"done"}'
    assert job.structured_result == '{"answer":"done"}'
    assert registry.store.usage_totals()["input_tokens"] == 7


def test_two_turns_in_one_batch_answer_two_jobs_in_order(registry):
    """Two boundaries, two waiting callers, each gets its own turn's text."""
    observer, _screen, clock, p, jobs = poised(registry)
    time.sleep(0.002)  # created_at is a float clock; keep the order unambiguous
    jobs.create(handle="h2", caller_id="caller", target_id=p.id, kind="send")
    batch = Batch(
        events=[
            said("first", turn_end=True),
            spoke("next question"),
            said("second", turn_end=True),
        ]
    )
    observer._apply(p.id, batch, clock, TurnAccumulator())
    assert jobs.get("h1").result == "first"
    assert jobs.get("h2").result == "second"


def test_one_turn_answers_only_the_caller_that_waited_longest(registry):
    """A queued second caller keeps waiting for its own turn, not this one."""
    observer, _screen, clock, p, jobs = poised(registry)
    time.sleep(0.002)
    jobs.create(handle="h2", caller_id="other", target_id=p.id, kind="send")
    observer._apply(p.id, Batch(events=[said("for h1", turn_end=True)]), clock, TurnAccumulator())
    assert jobs.get("h1").result == "for h1"
    assert str(jobs.get("h2").state) == "running"


def test_structured_undelivered_job_is_unavailable(registry):
    observer, _screen, clock, p, jobs = poised(
        registry,
        prompt="the injected prompt",
        response_format="json",
    )
    batch = Batch(
        events=[
            spoke("someone else's prompt"),
            said('{"answer": 1}', turn_end=True),
            spoke("another outside prompt"),
            said('{"answer": 2}', turn_end=True),
        ]
    )
    observer._apply(p.id, batch, clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "crashed"
    assert job.error_code == "prompt_never_seen"
    assert job.structured_result is None
    assert job.structured_status == "unavailable"
