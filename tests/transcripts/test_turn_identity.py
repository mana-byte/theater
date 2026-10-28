"""Adapters report each turn's own id, and the observer answers a named turn exactly once."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tests.support.observation import apply_batch
from tests.support.opencode_recorder import Recorder, text
from tests.support.paths import FIXTURES
from tests.support.shipped import ClaudeCodeObserver, CodexObserver, OpenCodeObserver, VibeObserver
from theater.daemon.jobs import JobManager
from theater.daemon.observer import (
    _ANSWERED_TURNS,
    Observer,
    QuietClock,
    TurnAccumulator,
)
from theater.harness.base import Event, EventKind
from theater.harness.source import Batch

# tests/fixtures/turn_* are verbatim captures of one injected round-trip per harness. Claude splits
# one message across records and repeats `stop_reason`, so the id keeps one reply from finishing
# two waiting jobs.


def events_for(observer, path: Path) -> list[Event]:
    out: list[Event] = []
    for i, line in enumerate(path.read_text().splitlines()):
        out.extend(observer.parse(line, i))
    return out


def boundaries(events: list[Event]) -> list[Event]:
    return [e for e in events if e.turn_end]


# ---- what each harness calls a turn ------------------------------------


def test_codex_reports_the_turn_id_it_stamps_on_both_ends():
    """`task_started` and `task_complete` carry the same `payload.turn_id`.

    Which makes the boundary joinable to its own beginning with no inference
    at all — the only harness of the four that gives us that outright.
    """
    records = [
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": "done",
            },
        },
    ]
    events = [
        event
        for index, record in enumerate(records)
        for event in CodexObserver().parse(json.dumps(record), index)
    ]
    ends = boundaries(events)

    assert len(ends) == 1
    assert ends[0].turn_id == "turn-1"


def test_claude_reports_the_message_id_the_split_records_share():
    """One message, several records, one id.

    `message.id` is what the duplicate boundary records have in common, so it
    is the only field that can be used to recognise the duplicate. The record
    `uuid` differs per record and would name each half a separate turn.
    """
    events = events_for(ClaudeCodeObserver(), FIXTURES / "turn_claude.jsonl")
    ends = boundaries(events)

    records = [
        json.loads(line) for line in (FIXTURES / "turn_claude.jsonl").read_text().splitlines()
    ]
    assistant = next(r for r in records if r.get("type") == "assistant")

    assert len(ends) == 1
    assert ends[0].turn_id == assistant["message"]["id"]


def test_vibe_uses_the_user_message_id_as_its_turn_identity():
    records = [json.loads(line) for line in (FIXTURES / "turn_vibe.jsonl").read_text().splitlines()]
    observer = VibeObserver()
    events = [
        event
        for index, record in enumerate(records)
        for event in observer.parse(json.dumps(record), index)
    ]
    ends = boundaries(events)

    assert len(ends) == 1
    assert ends[0].turn_id == records[0]["message_id"]


# ---- opencode, whose transcript is a database --------------------------


def test_opencode_names_the_turn_after_the_finished_message(tmp_path, workdir):
    recorder = Recorder(tmp_path / "opencode.db", "ses_one", str(workdir))
    source = OpenCodeObserver(db=recorder.path).open_source(cwd=str(workdir))
    try:
        assert asyncio.run(source.read()).attached is not None
        source.commit_attachment()
        recorder.step(
            "message-1",
            recorder.tick(),
            [text("answer")],
            finish="stop",
            times={"completed": recorder.tick()},
        )
        recorder.idle("idle-1", "succeeded")
        ends = boundaries(list(asyncio.run(source.read()).events))
        assert len(ends) == 1
        assert ends[0].turn_id == "message-1"
    finally:
        asyncio.run(source.aclose())
        recorder.conn.close()


# ---- the observer answers a named turn once ----------------------------


def poised(registry, *, jobs_wanted=1):
    """A participant with `jobs_wanted` callers queued behind it."""
    jobs = JobManager(registry.store)
    p = registry.register(harness="claude", pane=None, cwd="/tmp")
    for n in range(1, jobs_wanted + 1):
        jobs.create(handle=f"h{n}", caller_id=f"caller{n}", target_id=p.id, kind="send")
    return Observer(registry, harnesses={}, jobs=jobs), p, jobs


def ended(text: str, turn_id: str | None) -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, turn_end=True, turn_id=turn_id)


def test_one_message_announced_twice_answers_one_caller(registry):
    """Claude's split records, and the bug they caused.

    Two boundary events, same `message.id`, two callers waiting. The second
    caller's prompt has not been read yet, so handing it this reply would be
    answering a question the agent has not seen.
    """
    observer, p, jobs = poised(registry, jobs_wanted=2)
    batch = Batch(events=[ended("the reply", "msg_01"), ended("the reply", "msg_01")])

    apply_batch(observer, p.id, batch, QuietClock(), TurnAccumulator())

    assert jobs.get("h1").result == "the reply"
    assert str(jobs.get("h2").state) == "running"


def test_the_duplicate_is_still_ignored_a_poll_later(registry):
    """The two records need not land in the same batch.

    A poll can cut between them, which is precisely why the memory of answered
    turns outlives the batch rather than being rebuilt inside `_apply`.
    """
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()

    apply_batch(observer, p.id, Batch(events=[ended("the reply", "m1")]), clock, turns)
    apply_batch(observer, p.id, Batch(events=[ended("the reply", "m1")]), clock, turns)

    assert jobs.get("h1").result == "the reply"
    assert str(jobs.get("h2").state) == "running"


def test_two_genuinely_different_turns_answer_two_callers(registry):
    """The dedup must not swallow a real second reply."""
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()

    apply_batch(observer, p.id, Batch(events=[ended("first", "m1")]), clock, turns)
    apply_batch(observer, p.id, Batch(events=[ended("second", "m2")]), clock, turns)

    assert jobs.get("h1").result == "first"
    assert jobs.get("h2").result == "second"


def test_unidentified_boundaries_are_never_treated_as_duplicates(registry):
    """Vibe publishes no id. Two boundaries are two turns, always.

    The safe direction: a missed dedup answers the right caller twice over, a
    wrong one leaves a caller waiting for the 60s rescue.
    """
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()

    apply_batch(observer, p.id, Batch(events=[ended("same words", None)]), clock, turns)
    apply_batch(observer, p.id, Batch(events=[ended("same words", None)]), clock, turns)

    assert jobs.get("h1").result == "same words"
    assert jobs.get("h2").result == "same words"


# ---- the accumulator ---------------------------------------------------


def test_text_from_an_earlier_poll_still_answers_the_boundary(registry):
    """The turn spoke in one batch and ended in the next.

    Codex writes its reply as an `AgentMessage` item and ends the turn on a
    separate `task_complete` record; a poll landing between the two used to resolve the
    job with an empty string.
    """
    observer, p, jobs = poised(registry)
    clock, turns = QuietClock(), TurnAccumulator()
    spoke = Event(kind=EventKind.ASSISTANT, text="what it said", turn_end=False)

    apply_batch(observer, p.id, Batch(events=[spoke]), clock, turns)
    apply_batch(observer, p.id, Batch(events=[ended("", "m1")]), clock, turns)

    assert jobs.get("h1").result == "what it said"


def test_a_reply_written_in_blocks_comes_back_whole(registry):
    """Claude writes one content block per record. All of them are the reply.

    Keeping only the last block returned the closing paragraph of a long
    answer and dropped everything above it.
    """
    observer, p, jobs = poised(registry)
    batch = Batch(
        events=[
            Event(kind=EventKind.ASSISTANT, text="first para", turn_end=False),
            Event(kind=EventKind.ASSISTANT, text="second para", turn_end=False),
            ended("", "m1"),
        ]
    )

    apply_batch(observer, p.id, batch, QuietClock(), TurnAccumulator())

    assert jobs.get("h1").result == "first para\n\nsecond para"


def test_a_boundary_that_carries_its_own_text_is_believed(registry):
    """Codex repeats the whole reply on `task_complete`.

    Joining that to the accumulated preamble would hand the caller the same
    words twice, so the boundary's own text wins wherever it has any.
    """
    observer, p, jobs = poised(registry)
    batch = Batch(
        events=[
            Event(kind=EventKind.ASSISTANT, text="the answer", turn_end=False),
            ended("the answer", "t1"),
        ]
    )

    apply_batch(observer, p.id, batch, QuietClock(), TurnAccumulator())

    assert jobs.get("h1").result == "the answer"


def test_the_previous_turn_does_not_leak_into_the_next(registry):
    """Text is cleared at every boundary, answered or duplicate.

    Otherwise a caller who asked the second question receives the first
    answer stapled to the front of its own.
    """
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()

    apply_batch(observer, p.id, Batch(events=[ended("first answer", "m1")]), clock, turns)
    apply_batch(
        observer,
        p.id,
        Batch(
            events=[
                Event(kind=EventKind.ASSISTANT, text="second answer", turn_end=False),
                ended("", "m2"),
            ]
        ),
        clock,
        turns,
    )

    assert jobs.get("h2").result == "second answer"


def test_a_duplicate_boundary_does_not_carry_text_into_the_next_turn(registry):
    """The unanswered duplicate still consumes what the turn said.

    If it did not, the text of turn one would still be sitting in the
    accumulator when turn two ended and would be prepended to it.
    """
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()
    batch = Batch(
        events=[
            Event(kind=EventKind.ASSISTANT, text="turn one", turn_end=False),
            ended("", "m1"),
            ended("", "m1"),  # the duplicate record
            Event(kind=EventKind.ASSISTANT, text="turn two", turn_end=False),
            ended("", "m2"),
        ]
    )

    apply_batch(observer, p.id, batch, clock, turns)

    assert jobs.get("h1").result == "turn one"
    assert jobs.get("h2").result == "turn two"


def test_the_memory_of_answered_turns_is_bounded():
    """A watcher lives as long as its participant. This set must not grow.

    Only adjacent duplicates need catching, so forgetting the oldest ids is
    free — and unbounded growth over a day-long session is not.
    """
    turns = TurnAccumulator()
    for n in range(_ANSWERED_TURNS * 3):
        turns.mark_handled(f"turn-{n}")

    assert turns.already_handled(f"turn-{_ANSWERED_TURNS * 3 - 1}") is True
    assert turns.already_handled("turn-0") is False
    assert len(turns._seen) == _ANSWERED_TURNS
