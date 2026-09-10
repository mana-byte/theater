"""OpenCode turn-completion fidelity, audited against the native source.

The reproductions this file pins down come from reading the native tree
(/Users/manaiki.laut/Desktop/coding_clis/opencode, read-only):

- ``session/prompt.ts`` ends the loop for a finish reason other than
  ``tool-calls`` and ``unknown`` — an ``unknown`` step finish continues the
  turn, so it must never resolve an awaiting caller.
- ``session/processor.ts`` halts a failed turn by storing a message ``error``
  and setting the session idle *without writing any finish*; cleanup then
  persists ``time.completed``. A naive "finish means done" reader emits no
  terminal event for that row and the participant waits for rescue forever.
- Transient states leave no terminal marker on the message at all: retries
  only move session status, and an auto-compaction overflow persists
  ``time.completed`` with neither finish nor error while the loop continues
  with a fresh assistant message.

Everything runs against a real SQLite file with the four tables opencode
writes, in both shapes the adapter reads: the event log (`read()`) and the
stored message rows (`history()`), which must agree.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from shipped import OpenCodeHarness, OpenCodeObserver

from theater.harness import EventKind
from theater.models import Status

SCHEMA = """
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


class Recorder:
    """Writes a session the way opencode does: events plus current state."""

    def __init__(self, path: Path, sid: str, directory: str, created: int = 1000):
        self.path = path
        self.sid = sid
        self.seq = -1
        self.clock = created
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO session (id, parent_id, directory, time_created) VALUES (?, NULL, ?, ?)",
            (sid, str(Path(directory).resolve()), created),
        )
        self.conn.commit()

    def tick(self, ms: int = 10) -> int:
        self.clock += ms
        return self.clock

    def emit(self, kind: str, data: dict) -> None:
        self.seq += 1
        self.conn.execute(
            "INSERT INTO event (aggregate_id, seq, type, data) VALUES (?, ?, ?, ?)",
            (self.sid, self.seq, kind, json.dumps(data)),
        )
        self.conn.commit()

    def _store_message(self, mid: str, info: dict) -> None:
        self.conn.execute(
            "INSERT INTO message (id, session_id, time_created, data) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (mid, self.sid, info["time"]["created"], json.dumps(info)),
        )
        self.conn.commit()

    def message(self, mid: str, role: str) -> dict:
        info = {"id": mid, "role": role, "time": {"created": self.tick()}}
        self.emit("message.updated.1", {"info": info})
        self._store_message(mid, info)
        return info

    def text(self, mid: str, pid: str, body: str) -> None:
        part = {"id": pid, "messageID": mid, "type": "text", "text": body}
        when = self.tick()
        self.emit("message.part.updated.1", {"part": part, "time": when})
        self.conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (pid, mid, self.sid, when, json.dumps(part)),
        )
        self.conn.commit()

    def update(self, info: dict, **changes: object) -> dict:
        """One message.updated event plus the stored row, as opencode writes."""
        info = dict(info, **changes)
        self.emit("message.updated.1", {"info": info})
        self._store_message(info["id"], info)
        return info


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def rec(tmp_path, workdir):
    r = Recorder(tmp_path / "opencode-audit.db", "ses_one", str(workdir))
    yield r
    r.conn.close()


def source(rec, workdir, **kwargs):
    return OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir), **kwargs)


def attach(rec, workdir):
    """Open live, accept the attachment, and read from the head."""
    src = source(rec, workdir)
    batch = asyncio.run(src.read())
    assert batch.attached is not None
    src.commit_attachment()
    return src


def read_events(src) -> list:
    return asyncio.run(src.read()).events


# ---- an unknown step finish continues the turn ----------------------------


def test_an_unknown_finish_is_a_step_not_a_turn_end(rec, workdir):
    """prompt.ts excludes both `tool-calls` and `unknown` when deciding a
    turn ended, so an `unknown` step must read as a step: a snapshot with
    turn_end False, and a caller's await still waiting."""
    src = attach(rec, workdir)
    user = rec.message("msg_u1", "user")
    rec.text(user["id"], "prt_u1", "go on then")
    step = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "intermediate words")
    rec.update(step, finish="unknown", time=dict(step["time"], completed=rec.tick()))

    events = read_events(src)

    assert [e.kind for e in events] == [EventKind.USER, EventKind.ASSISTANT]
    assert events[1].turn_end is False
    assert events[1].text == "intermediate words"


def test_a_turn_that_continued_through_unknown_still_ends(rec, workdir):
    """The step after an `unknown` one is a fresh assistant message; its
    terminal finish is the event that ends the turn."""
    src = attach(rec, workdir)
    step = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "one")
    rec.update(step, finish="unknown", time=dict(step["time"], completed=rec.tick()))
    last = rec.message("msg_a2", "assistant")
    rec.text("msg_a2", "prt_t2", "two")
    rec.update(last, finish="stop", time=dict(last["time"], completed=rec.tick()))

    events = read_events(src)

    assert [(e.turn_id, e.turn_end) for e in events if e.kind is EventKind.ASSISTANT] == [
        ("msg_a1", False),
        ("msg_a2", True),
    ]


def test_an_unknown_finish_keeps_the_session_status_working(rec, workdir):
    step = rec.message("msg_a1", "assistant")
    rec.update(step, finish="unknown", time=dict(step["time"], completed=rec.tick()))

    assert asyncio.run(source(rec, workdir).read()).status is Status.WORKING


# ---- a stored error ends the turn without any finish ----------------------


def test_a_terminal_error_without_finish_ends_the_turn(rec, workdir):
    """processor.ts halt stores a message error and idles the session without
    writing a finish; cleanup only later persists time.completed. This row
    used to produce zero events and left the participant waiting for rescue."""
    src = attach(rec, workdir)
    user = rec.message("msg_u1", "user")
    rec.text(user["id"], "prt_u1", "try this")
    failed = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "partial answer")
    rec.update(
        failed,
        error={"name": "APIError", "message": "rate limited"},
        time=dict(failed["time"], completed=rec.tick()),
    )

    events = read_events(src)

    # The partial content is a step; the failure detail is the boundary. An
    # ERROR event does not feed the turn accumulator, so the text is said
    # once and the awaiting caller's answer keeps the native error.
    assert [(e.kind, e.turn_end) for e in events] == [
        (EventKind.USER, False),
        (EventKind.ASSISTANT, False),
        (EventKind.ERROR, True),
    ]
    assert events[1].text == "partial answer"
    assert events[2].text == "APIError: rate limited"


def test_a_terminal_error_with_no_text_still_ends_the_turn(rec, workdir):
    """A turn that failed before producing any visible output still needs
    its terminal event — and the ERROR event carries the native error, so
    an awaiting caller resolves with the failure instead of a blank
    success."""
    src = attach(rec, workdir)
    failed = rec.message("msg_a1", "assistant")
    rec.update(
        failed,
        error={"name": "APIError", "message": "no key"},
        time=dict(failed["time"], completed=rec.tick()),
    )

    events = read_events(src)

    assert len(events) == 1
    assert events[0].turn_end is True
    assert events[0].kind is EventKind.ERROR
    assert events[0].text == "APIError: no key"


def test_a_stored_error_means_idle(rec, workdir):
    failed = rec.message("msg_a1", "assistant")
    rec.update(
        failed,
        error={"name": "APIError", "message": "rate limited"},
        time=dict(failed["time"], completed=rec.tick()),
    )

    assert asyncio.run(source(rec, workdir).read()).status is Status.IDLE


def test_an_error_after_a_tool_calls_finish_still_ends_the_turn(rec, workdir):
    """An aborted tool call lands as an error on a message whose finish is
    already `tool-calls`. The step snapshot was emitted, but the stored
    error must still be able to end the turn afterwards."""
    src = attach(rec, workdir)
    step = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "about to run a tool")
    rec.update(step, finish="tool-calls", time=dict(step["time"], completed=rec.tick()))
    snapshot = read_events(src)
    assert [(e.kind, e.turn_end) for e in snapshot] == [(EventKind.ASSISTANT, False)]

    rec.update(
        step,
        error={"name": "AbortedError", "message": "Aborted"},
        time=dict(step["time"], completed=rec.tick()),
    )
    terminal = read_events(src)

    # Only previously unreported content plus the terminal signal: the step
    # text is not repeated, the failure detail is the boundary.
    assert [(e.kind, e.turn_end) for e in terminal] == [(EventKind.ERROR, True)]
    assert terminal[0].text == "AbortedError: Aborted"
    assert terminal[0].usage is None


# ---- transients must not complete early ----------------------------------


def test_a_completed_message_without_finish_or_error_is_still_working(rec, workdir):
    """Both a retrying turn and an auto-compaction overflow persist
    time.completed on the message with no finish and no error; the loop
    continues with a fresh assistant message. This row must not end anything."""
    src = attach(rec, workdir)
    user = rec.message("msg_u1", "user")
    rec.text(user["id"], "prt_u1", "hello")
    overflowing = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "a long answer")
    rec.update(overflowing, time=dict(overflowing["time"], completed=rec.tick()))

    events = read_events(src)
    assert [e.kind for e in events] == [EventKind.USER]
    assert asyncio.run(source(rec, workdir).read()).status is Status.WORKING


def test_a_retry_then_terminal_failure_only_ends_once(rec, workdir):
    """Retries leave the message untouched, the terminal failure stores the
    error once, and repeated message updates after it are duplicates."""
    src = attach(rec, workdir)
    failed = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "partial")
    # The retry storm: session status events, no message mutation.
    rec.emit("session.updated.1", {"info": {"id": rec.sid, "status": "retry"}})
    assert read_events(src) == []
    # Cleanup persists the message error once...
    rec.update(
        failed,
        error={"name": "APIError", "message": "gave up"},
        time=dict(failed["time"], completed=rec.tick()),
    )
    # ...and native (or a slow watcher) may repeat the same row afterwards.
    rec.update(
        failed,
        error={"name": "APIError", "message": "gave up"},
        time=dict(failed["time"], completed=rec.tick()),
    )
    rec.update(
        failed,
        error={"name": "APIError", "message": "gave up"},
        time=dict(failed["time"], completed=rec.tick()),
    )

    events = read_events(src)

    assert [(e.kind, e.turn_end) for e in events] == [
        (EventKind.ASSISTANT, False),
        (EventKind.ERROR, True),
    ]
    assert events[0].text == "partial"
    assert events[1].text == "APIError: gave up"
    # The repeated rows add nothing — no duplicate text, no duplicate usage.
    assert not read_events(src)


def test_repeated_terminal_finishes_emit_one_turn_end(rec, workdir):
    """`finish` fires twice — once bare, once with time.completed — and the
    daemon must see exactly one terminal event for the turn."""
    src = attach(rec, workdir)
    step = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "done")
    rec.update(step, finish="stop")
    rec.update(step, finish="stop", time=dict(step["time"], completed=rec.tick()))
    rec.update(step, finish="stop", time=dict(step["time"], completed=rec.tick()))

    events = read_events(src)

    assert [e.turn_end for e in events] == [True]


# ---- streaming and history agree ------------------------------------------


def test_history_classifies_an_unknown_finish_as_a_step(rec, workdir):
    step = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "one")
    rec.update(step, finish="unknown", time=dict(step["time"], completed=rec.tick()))
    last = rec.message("msg_a2", "assistant")
    rec.text("msg_a2", "prt_t2", "two")
    rec.update(last, finish="stop", time=dict(last["time"], completed=rec.tick()))

    history = asyncio.run(source(rec, workdir).history(last_n=0))

    assert [(e.turn_id, e.turn_end) for e in history.events] == [
        ("msg_a1", False),
        ("msg_a2", True),
    ]


def test_history_ends_a_turn_on_a_stored_error_without_finish(rec, workdir):
    failed = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "partial answer")
    rec.update(
        failed,
        error={"name": "APIError", "message": "rate limited"},
        time=dict(failed["time"], completed=rec.tick()),
    )

    history = asyncio.run(source(rec, workdir).history(last_n=0))

    # The same two events the live path emits for the same stored row.
    assert [(e.kind, e.turn_end) for e in history.events] == [
        (EventKind.ASSISTANT, False),
        (EventKind.ERROR, True),
    ]
    assert history.events[0].text == "partial answer"
    assert history.events[1].text == "APIError: rate limited"


def test_an_aborted_tool_call_does_not_duplicate_the_step_text(rec, workdir):
    """The audit's duplicate, end to end: a step snapshot for `tool-calls`,
    then the same message carrying the stored abort error. The step text is
    emitted once, the terminal event carries only the failure detail plus
    the boundary, and a cold replay of the same rows produces the same
    events — text, boundary, and usage placement included."""
    src = attach(rec, workdir)
    step = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "about to run a tool")
    rec.update(
        step,
        finish="tool-calls",
        tokens={"input": 5, "output": 3},
        time=dict(step["time"], completed=rec.tick()),
    )
    step_events = read_events(src)
    rec.update(
        step,
        error={"name": "AbortedError", "message": "Aborted"},
        tokens={"input": 5, "output": 3},
        time=dict(step["time"], completed=rec.tick()),
    )
    live = step_events + read_events(src)

    assert [(e.kind, e.text, e.turn_end) for e in live] == [
        (EventKind.ASSISTANT, "about to run a tool", False),
        (EventKind.ERROR, "AbortedError: Aborted", True),
    ]
    # One usage report in the stream: the snapshot carried it, the terminal
    # event does not repeat it.
    carried = [e.usage for e in live if e.usage is not None]
    assert len(carried) == 1
    assert carried[0].input_tokens == 5
    assert carried[0].output_tokens == 3

    stored = asyncio.run(src.history(last_n=0)).events
    assert [(e.kind, e.text, e.turn_end) for e in stored] == [
        (e.kind, e.text, e.turn_end) for e in live
    ]
    assert [e.usage for e in stored if e.usage is not None] == [
        e.usage for e in live if e.usage is not None
    ]


def test_final_accumulated_text_and_tokens_match_cold_history(rec, workdir):
    """Accumulation, not event kinds: a two-step turn that fails on the
    second step. What the daemon's turn accumulator would hold at the
    boundary — ASSISTANT text blocks joined, ERROR events silent, usage
    summed (daemon/observation/reducer.py: ASSISTANT feeds turns.say, ERROR
    does not, and a non-empty boundary event answers with its own text) —
    is identical computed live and from cold history."""
    src = attach(rec, workdir)
    user = rec.message("msg_u1", "user")
    rec.text(user["id"], "prt_u1", "do the thing")
    one = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "step one")
    rec.update(
        one,
        finish="tool-calls",
        tokens={"input": 10, "output": 2},
        time=dict(one["time"], completed=rec.tick()),
    )
    two = rec.message("msg_a2", "assistant")
    rec.text("msg_a2", "prt_t2", "step two, failing")
    rec.update(
        two,
        error={"name": "APIError", "message": "rate limited"},
        tokens={"input": 7, "output": 4},
        time=dict(two["time"], completed=rec.tick()),
    )
    live = read_events(src)
    stored = asyncio.run(src.history(last_n=0)).events

    def accumulated(events):
        said = [
            e.text
            for e in events
            if e.kind is EventKind.ASSISTANT and e.turn_end is False and e.text
        ]
        boundary = [e for e in events if e.turn_end]
        assert len(boundary) == 1
        answer = boundary[0].text if boundary[0].text else "\n\n".join(said)
        tokens_in = sum(e.usage.input_tokens for e in events if e.usage)
        tokens_out = sum(e.usage.output_tokens for e in events if e.usage)
        return ("\n\n".join(said), answer, tokens_in, tokens_out)

    assert accumulated(live) == accumulated(stored)
    assert accumulated(live) == (
        "step one\n\nstep two, failing",
        "APIError: rate limited",
        17,
        6,
    )


def test_live_and_history_agree_on_an_error_only_turn(rec, workdir):
    src = attach(rec, workdir)
    failed = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "partial answer")
    rec.update(
        failed,
        error={"name": "APIError", "message": "rate limited"},
        time=dict(failed["time"], completed=rec.tick()),
    )

    live = read_events(src)
    stored = asyncio.run(src.history(last_n=0)).events

    assert [(e.kind, e.text, e.turn_end) for e in live] == [
        (e.kind, e.text, e.turn_end) for e in stored
    ]


def test_an_unknown_finish_is_a_partial_trajectory_step():
    """The trajectory projection must not present an `unknown` step as a
    completed assistant record while the native loop is still running."""
    from theater.harness.builtin.plugins.opencode.values import _finish_status
    from theater.trajectory.enums import TrajectoryStatus

    assert _finish_status("tool-calls") is TrajectoryStatus.PARTIAL
    assert _finish_status("unknown") is TrajectoryStatus.PARTIAL
    assert _finish_status("stop") is TrajectoryStatus.COMPLETED
    assert _finish_status(None) is TrajectoryStatus.RUNNING


def test_the_launch_plan_still_carries_the_configured_database(rec, tmp_path):
    """The audited completion semantics live in the source; the plan keeps
    binding the observation database and the plugin the same way."""
    plan = OpenCodeHarness(db=rec.path).plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="manual",
    )
    assert plan.env["OPENCODE_DB"] == str(rec.path.resolve())
