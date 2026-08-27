"""Naming a turn, and answering it exactly once.

Two halves, and the seam between them is `Event.turn_id`.

Below the seam, each adapter reports what its harness already calls the turn.
That is not inference: three of the four publish an id of their own, and the
fixtures here are verbatim captures of one real injected round-trip per
harness — `tests/fixtures/turn_*`, taken through the daemon against the live
CLIs, not hand-written. A parser that stops agreeing with its harness fails
here rather than in production, which is the whole reason the captures exist.

Above the seam, the observer uses the id for one thing: answering a turn once.
Claude splits a single message across several records and repeats
`stop_reason` on each, so one reply used to finish two waiting jobs — the
second caller receiving the first caller's answer, instantly, before its own
prompt had even been read.

The accumulator tests belong here too, because they are the same bug seen from
the other side: a boundary is only as good as the text it can hand back, and
batches are cut wherever the poll landed rather than where the turn did.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from shipped import ClaudeCodeObserver, CodexObserver, OpenCodeObserver, VibeObserver

from theater.daemon.jobs import JobManager
from theater.daemon.observer import (
    _ANSWERED_TURNS,
    Observer,
    QuietClock,
    TurnAccumulator,
)
from theater.harness.base import Event, EventKind
from theater.harness.source import Batch

FIXTURES = Path(__file__).parent / "fixtures"


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
    events = events_for(CodexObserver(), FIXTURES / "turn_codex.jsonl")
    ends = boundaries(events)

    started = json.loads((FIXTURES / "turn_codex.jsonl").read_text().splitlines()[1])
    assert started["payload"]["type"] == "task_started"

    assert len(ends) == 1
    assert ends[0].turn_id == started["payload"]["turn_id"]


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

OPENCODE_SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_id TEXT, seq INTEGER,
    type TEXT, data TEXT
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
    time_created INTEGER, time_updated INTEGER, data TEXT
);
"""


def replay_opencode_capture(db: Path, directory: str) -> list[Event]:
    """Put the captured rows back in a database and read them as they arrive.

    The capture is the three tables opencode actually wrote for one turn. They
    are inserted *after* the source attaches, because attaching skips whatever
    is already there — replaying them as history would exercise a different
    code path than the live one this is about.
    """
    rows = json.loads((FIXTURES / "turn_opencode.json").read_text())
    sid = rows["event"][0]["aggregate_id"]

    conn = sqlite3.connect(db)
    try:
        conn.executescript(OPENCODE_SCHEMA)
        conn.execute(
            "INSERT INTO session (id, parent_id, directory, time_created) "
            "VALUES (?, NULL, ?, 1000)",
            (sid, directory),
        )
        conn.commit()

        source = OpenCodeObserver(db=db).open_source(cwd=directory)
        asyncio.run(source.read())  # attach at the end of an empty log
        source.commit_attachment()

        for table in ("message", "part", "event"):
            # The captured `event.id` is opencode's own `evt_…` string and the
            # local column is the autoincrementing integer the rest of the
            # suite uses. Dropped rather than reconciled: the adapter orders
            # the log by `seq` and never looks at the row id.
            drop = {"id"} if table == "event" else set()
            for raw in rows[table]:
                row = {k: v for k, v in raw.items() if k not in drop}
                columns = ", ".join(row)
                marks = ", ".join("?" * len(row))
                conn.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({marks})",
                    tuple(row.values()),
                )
        conn.commit()

        batch = asyncio.run(source.read())
        asyncio.run(source.aclose())
        return list(batch.events)
    finally:
        conn.close()


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


def test_opencode_names_the_turn_after_the_finished_message(tmp_path, workdir):
    """A turn ends when a message finishes, so the message id names the turn.

    Every part of the reply already references it as `messageID`; nothing else
    on the row is a candidate.
    """
    events = replay_opencode_capture(tmp_path / "opencode-stable.db", str(workdir))
    ends = boundaries(events)

    rows = json.loads((FIXTURES / "turn_opencode.json").read_text())
    assistant = next(r for r in rows["message"] if json.loads(r["data"]).get("role") == "assistant")

    assert len(ends) == 1
    assert ends[0].turn_id == assistant["id"]


# ---- the observer answers a named turn once ----------------------------


def poised(registry, *, jobs_wanted=1):
    """A participant with `jobs_wanted` callers queued behind it."""
    jobs = JobManager(registry.store)
    p = registry.register(harness="claude", pane="%1", cwd="/tmp")
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

    observer._apply(p.id, batch, QuietClock(), TurnAccumulator())

    assert jobs.get("h1").result == "the reply"
    assert str(jobs.get("h2").state) == "running"


def test_the_duplicate_is_still_ignored_a_poll_later(registry):
    """The two records need not land in the same batch.

    A poll can cut between them, which is precisely why the memory of answered
    turns outlives the batch rather than being rebuilt inside `_apply`.
    """
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()

    observer._apply(p.id, Batch(events=[ended("the reply", "m1")]), clock, turns)
    observer._apply(p.id, Batch(events=[ended("the reply", "m1")]), clock, turns)

    assert jobs.get("h1").result == "the reply"
    assert str(jobs.get("h2").state) == "running"


def test_two_genuinely_different_turns_answer_two_callers(registry):
    """The dedup must not swallow a real second reply."""
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()

    observer._apply(p.id, Batch(events=[ended("first", "m1")]), clock, turns)
    observer._apply(p.id, Batch(events=[ended("second", "m2")]), clock, turns)

    assert jobs.get("h1").result == "first"
    assert jobs.get("h2").result == "second"


def test_unidentified_boundaries_are_never_treated_as_duplicates(registry):
    """Vibe publishes no id. Two boundaries are two turns, always.

    The safe direction: a missed dedup answers the right caller twice over, a
    wrong one leaves a caller waiting for the 60s rescue.
    """
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()

    observer._apply(p.id, Batch(events=[ended("same words", None)]), clock, turns)
    observer._apply(p.id, Batch(events=[ended("same words", None)]), clock, turns)

    assert jobs.get("h1").result == "same words"
    assert jobs.get("h2").result == "same words"


# ---- the accumulator ---------------------------------------------------


def test_text_from_an_earlier_poll_still_answers_the_boundary(registry):
    """The turn spoke in one batch and ended in the next.

    Codex writes its reply as `agent_message` and ends the turn on a separate
    `task_complete` record; a poll landing between the two used to resolve the
    job with an empty string.
    """
    observer, p, jobs = poised(registry)
    clock, turns = QuietClock(), TurnAccumulator()
    spoke = Event(kind=EventKind.ASSISTANT, text="what it said", turn_end=False)

    observer._apply(p.id, Batch(events=[spoke]), clock, turns)
    observer._apply(p.id, Batch(events=[ended("", "m1")]), clock, turns)

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

    observer._apply(p.id, batch, QuietClock(), TurnAccumulator())

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

    observer._apply(p.id, batch, QuietClock(), TurnAccumulator())

    assert jobs.get("h1").result == "the answer"


def test_the_previous_turn_does_not_leak_into_the_next(registry):
    """Text is cleared at every boundary, answered or duplicate.

    Otherwise a caller who asked the second question receives the first
    answer stapled to the front of its own.
    """
    observer, p, jobs = poised(registry, jobs_wanted=2)
    clock, turns = QuietClock(), TurnAccumulator()

    observer._apply(p.id, Batch(events=[ended("first answer", "m1")]), clock, turns)
    observer._apply(
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

    observer._apply(p.id, batch, clock, turns)

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
