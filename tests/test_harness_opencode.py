"""The first adapter whose input is a database rather than a file.

Everything here runs against a real SQLite file built by `Recorder`, which
writes the same four tables and the same event payloads opencode does. The
shapes were taken from a live `~/.local/share/opencode/opencode-stable.db`
after driving a session that asked a question, ran a tool, and answered — the
two-step turn is what most of these assertions are about, since a step boundary
that reads as a turn boundary would resolve a caller's `await_sessions` with an
empty answer.

Fidelity that matters, and is therefore reproduced exactly:
  - `finish` fires twice per message, the second time with `time.completed`;
  - `finish == "tool-calls"` ends a step, and a new assistant message follows;
  - text parts arrive empty and are then replaced by the whole text;
  - tool parts go pending -> running -> completed;
  - `session.directory` is a resolved path and every timestamp is in ms.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from shipped import OpenCodeHarness, OpenCodeObserver

from theater.harness import EventKind
from theater.models import BadRequest, Status

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
    """Writes a session the way opencode does: events plus current state.

    Both are kept in step because the adapter reads both — `read()` follows the
    event log, `history()` reads the message and part tables — and a fixture
    that let them disagree would let a bug in either go unnoticed.
    """

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

    # ---- plumbing -------------------------------------------------------

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

    def _store_part(self, part: dict, when: int) -> None:
        self.conn.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, data) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET data = excluded.data",
            (part["id"], part["messageID"], self.sid, when, json.dumps(part)),
        )
        self.conn.commit()

    def _part(self, part: dict) -> None:
        when = self.tick()
        self.emit("message.part.updated.1", {"part": part, "time": when})
        self._store_part(part, when)

    # ---- the shapes a session is made of --------------------------------

    def message(self, mid: str, role: str) -> dict:
        info = {"id": mid, "role": role, "time": {"created": self.tick()}}
        self.emit("message.updated.1", {"info": info})
        self._store_message(mid, info)
        return info

    def finish(self, info: dict, reason: str) -> None:
        """Both firings, in order, as opencode writes them."""
        info = dict(info, finish=reason)
        self.emit("message.updated.1", {"info": info})
        info = dict(info, time=dict(info["time"], completed=self.tick()))
        self.emit("message.updated.1", {"info": info})
        self._store_message(info["id"], info)

    def text(self, mid: str, pid: str, body: str) -> None:
        """Empty first, then replaced whole — never appended to."""
        self._part({"id": pid, "messageID": mid, "type": "text", "text": ""})
        self._part({"id": pid, "messageID": mid, "type": "text", "text": body})

    def user_text(self, mid: str, pid: str, body: str) -> None:
        """A user part arrives complete and only once."""
        self._part({"id": pid, "messageID": mid, "type": "text", "text": body})

    def tool(self, mid: str, pid: str, call: str, name: str, output: str) -> None:
        base = {
            "id": pid,
            "messageID": mid,
            "type": "tool",
            "callID": call,
            "tool": name,
        }
        self._part({**base, "state": {"status": "pending", "input": {}}})
        self._part({**base, "state": {"status": "running", "input": {"x": 1}}})
        self._part({**base, "state": {"status": "completed", "output": output}})

    def step_finish(self, mid: str, pid: str, reason: str) -> None:
        self._part({"id": pid, "messageID": mid, "type": "step-finish", "reason": reason})


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def rec(tmp_path, workdir):
    r = Recorder(tmp_path / "opencode-stable.db", "ses_one", str(workdir))
    yield r
    r.conn.close()


def source_for(rec, workdir, **kwargs):
    return OpenCodeObserver(db=rec.path).open_source(cwd=str(workdir), **kwargs)


def attach(src):
    """Stage and accept the initial attachment, as the observer does."""
    batch = asyncio.run(src.read())
    assert batch.attached is not None
    src.commit_attachment()
    return batch


def receipt_source(rec, workdir, tmp_path, *, participant_id="participant"):
    """Attach a source whose process-local plugin receipt owns ``ses_one``."""
    correlation = tmp_path / "correlation"
    correlation.mkdir()
    (correlation / f"{participant_id}.opencode.mjs").write_text("// marker\n")
    receipt = correlation / f"{participant_id}.opencode-session"
    receipt.write_text(
        json.dumps({"participant_id": participant_id, "session_id": "ses_one"}) + "\n"
    )
    src = OpenCodeObserver(db=rec.path, correlation_dir=correlation).open_source_for(
        participant_id=participant_id,
        cwd=str(workdir),
        after=0.0,
    )
    attach(src)
    return src, receipt


def a_turn_with_a_tool(rec) -> None:
    """The canonical trace: ask, call a tool, then answer in a second step."""
    user = rec.message("msg_u1", "user")
    rec.user_text(user["id"], "prt_u1", "read the note")
    first = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "")
    rec.tool("msg_a1", "prt_tool", "call_1", "read", "the secret is pamplemousse")
    rec.step_finish("msg_a1", "prt_sf1", "tool-calls")
    rec.finish(first, "tool-calls")
    second = rec.message("msg_a2", "assistant")
    rec.text("msg_a2", "prt_t2", "pamplemousse")
    rec.step_finish("msg_a2", "prt_sf2", "stop")
    rec.finish(second, "stop")


# ---- launching ----------------------------------------------------------


def test_the_id_travels_in_a_merged_config_file(tmp_path):
    config = tmp_path / "abc.json"
    plan = OpenCodeHarness().plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=config,
        approval="manual",
    )

    assert plan.argv == ["opencode", "--prompt", "say hello"]
    assert plan.env == {"OPENCODE_CONFIG": str(config)}
    document = json.loads(plan.files[config])
    server = document["mcp"]["theater"]
    assert server["command"][-3:] == ["mcp", "--id", "abc123"]
    assert server["enabled"] is True
    plugin = tmp_path / "abc.opencode.mjs"
    receipt = tmp_path / "abc.opencode-session"
    assert document["plugin"] == [plugin.resolve().as_uri()]
    assert plugin in plan.files
    assert "session.created" in plan.files[plugin]
    assert "event.properties.info.parentID" in plan.files[plugin]
    assert "abc123" in plan.files[plugin]
    assert str(receipt) in plan.files[plugin]
    assert receipt not in plan.files


def test_yolo_is_the_only_approval_flag_there_is(tmp_path):
    """`edits` degrades to `manual`: opencode has no middle ground."""

    def argv(approval):
        return (
            OpenCodeHarness()
            .plan_launch(
                participant_id="abc123",
                prompt="",
                config_path=tmp_path / "x.json",
                approval=approval,
            )
            .argv
        )

    assert argv("yolo") == ["opencode", "--auto"]
    assert argv("edits") == ["opencode"]
    assert argv("manual") == ["opencode"]


def test_an_unknown_approval_is_refused(tmp_path):
    with pytest.raises(BadRequest):
        OpenCodeHarness().plan_launch(
            participant_id="abc123",
            prompt="",
            config_path=tmp_path / "x.json",
            approval="whatever",
        )


# ---- attaching ----------------------------------------------------------


def test_a_missing_database_is_waiting_not_an_error(tmp_path, workdir):
    observer = OpenCodeObserver(db=tmp_path / "nope.db")
    batch = asyncio.run(observer.open_source(cwd=str(workdir)).read())
    assert batch.waiting is True


def test_a_directory_with_no_session_is_waiting(rec, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert asyncio.run(source_for(rec, other).read()).waiting is True


def test_attaching_skips_history_and_reports_where_it_started(rec, workdir):
    a_turn_with_a_tool(rec)
    src = source_for(rec, workdir)

    batch = asyncio.run(src.read())

    assert batch.attached.session_id == "ses_one"
    assert batch.attached.location == "opencode://ses_one"
    assert batch.attached.skipped == rec.seq + 1
    assert not batch.events
    src.commit_attachment()
    # Nothing new after an attach that skipped everything.
    assert not asyncio.run(src.read()).events


def test_a_session_that_finished_before_we_looked_is_idle(rec, workdir):
    """The reason `Batch.status` exists: no further event will ever arrive."""
    a_turn_with_a_tool(rec)
    assert asyncio.run(source_for(rec, workdir).read()).status is Status.IDLE


def test_a_session_still_working_when_we_attach_says_so(rec, workdir):
    user = rec.message("msg_u1", "user")
    rec.user_text(user["id"], "prt_u1", "hello")
    rec.message("msg_a1", "assistant")

    assert asyncio.run(source_for(rec, workdir).read()).status is Status.WORKING


def test_a_session_with_no_messages_is_idle(rec, workdir):
    """Launched without a prompt. Calling it WORKING would strand it there."""
    assert asyncio.run(source_for(rec, workdir).read()).status is Status.IDLE


def test_a_sub_agent_session_is_not_mistaken_for_the_pane(rec, workdir):
    """Sub-agents share their parent's directory and are always newer."""
    rec.conn.execute(
        "INSERT INTO session (id, parent_id, directory, time_created) "
        "VALUES ('ses_child', 'ses_one', ?, 9999)",
        (str(workdir.resolve()),),
    )
    rec.conn.commit()

    assert asyncio.run(source_for(rec, workdir).read()).attached.session_id == "ses_one"


def test_a_session_older_than_the_participant_is_not_ours(rec, workdir):
    """`after` is a floor for a spawn: an earlier session is someone else's."""
    batch = asyncio.run(source_for(rec, workdir, after=500.0).read())
    assert batch.waiting is True
    # `created` is 1000ms in the fixture, so a floor below it matches.
    assert asyncio.run(source_for(rec, workdir, after=0.5).read()).attached is not None


def test_process_receipts_disambiguate_concurrent_sessions_in_the_same_cwd(rec, workdir, tmp_path):
    """The Pasquina/Harpagon regression.

    Both participants' time/cwd searches match both rows, and the plain fallback
    would return ``ses_two`` for each. Their process-local receipts must produce
    the exact one-to-one binding instead, even if a stale registry id says the
    opposite session.
    """
    rec.conn.execute(
        "INSERT INTO session (id, parent_id, directory, time_created) "
        "VALUES ('ses_two', NULL, ?, 2000)",
        (str(workdir.resolve()),),
    )
    rec.conn.commit()
    correlation = tmp_path / "mcp"
    correlation.mkdir()
    for pid, sid in (("participant_a", "ses_one"), ("participant_b", "ses_two")):
        (correlation / f"{pid}.opencode.mjs").write_text("// capability marker\n")
        (correlation / f"{pid}.opencode-session").write_text(
            json.dumps({"participant_id": pid, "session_id": sid}) + "\n"
        )

    observer = OpenCodeObserver(db=rec.path, correlation_dir=correlation)
    source_a = observer.open_source_for(
        participant_id="participant_a",
        cwd=str(workdir),
        # Simulate the pre-fix daemon restarting with the wrong stored binding.
        session_id="ses_two",
        after=0.0,
    )
    source_b = observer.open_source_for(
        participant_id="participant_b",
        cwd=str(workdir),
        session_id="ses_one",
        after=0.0,
    )

    assert asyncio.run(source_a.read()).attached.session_id == "ses_one"
    assert asyncio.run(source_b.read()).attached.session_id == "ses_two"


def test_a_receipt_capable_process_waits_for_its_receipt(rec, workdir, tmp_path):
    """Never fall back during the creation-event/receipt race window."""
    correlation = tmp_path / "mcp"
    correlation.mkdir()
    (correlation / "participant.opencode.mjs").write_text("// capability marker\n")
    observer = OpenCodeObserver(db=rec.path, correlation_dir=correlation)

    batch = asyncio.run(
        observer.open_source_for(
            participant_id="participant",
            cwd=str(workdir),
            after=0.0,
        ).read()
    )

    assert batch.waiting is True
    assert batch.attached is None


# ---- reading ------------------------------------------------------------


def drain(rec, workdir):
    """Attach before anything happened, then read the whole session live."""
    src = source_for(rec, workdir)
    attach(src)
    return src


def test_a_turn_reads_as_one_user_one_tool_pair_and_one_reply(rec, workdir):
    src = drain(rec, workdir)
    a_turn_with_a_tool(rec)

    batch = asyncio.run(src.read())

    assert [e.kind for e in batch.events] == [
        EventKind.USER,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]
    assert batch.progressed is True
    assert batch.events[0].text == "read the note"
    assert batch.events[1].tool_name == "read"
    assert batch.events[2].text == "the secret is pamplemousse"
    assert batch.events[3].text == "pamplemousse"


def test_only_the_last_step_ends_the_turn(rec, workdir):
    """`finish == "tool-calls"` is a step. Ending the turn there would hand a
    waiting caller the empty text of a message that only called a tool."""
    src = drain(rec, workdir)
    a_turn_with_a_tool(rec)

    ends = [e.turn_end for e in asyncio.run(src.read()).events]

    assert ends == [False, False, False, True]


def test_a_step_that_only_called_tools_produces_no_assistant_line(rec, workdir):
    src = drain(rec, workdir)
    a_turn_with_a_tool(rec)

    said = [e for e in asyncio.run(src.read()).events if e.kind is EventKind.ASSISTANT]

    assert len(said) == 1


def test_the_finish_that_fires_twice_is_reported_once(rec, workdir):
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_t1", "hello")
    rec.finish(info, "stop")

    events = asyncio.run(src.read()).events

    assert [e.kind for e in events] == [EventKind.ASSISTANT]
    assert events[0].text == "hello"


def test_streamed_text_is_emitted_once_whole_at_the_end(rec, workdir):
    """Not once per update: the bus would carry three prefixes of one reply,
    and the job would resolve with whichever fragment landed last."""
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec._part({"id": "p", "messageID": "msg_a1", "type": "text", "text": "par"})
    rec._part({"id": "p", "messageID": "msg_a1", "type": "text", "text": "parti"})

    assert not asyncio.run(src.read()).events

    rec._part({"id": "p", "messageID": "msg_a1", "type": "text", "text": "partial"})
    rec.finish(info, "stop")
    events = asyncio.run(src.read()).events

    assert [(e.kind, e.text) for e in events] == [(EventKind.ASSISTANT, "partial")]


def test_a_reply_written_in_several_parts_is_joined_in_order(rec, workdir):
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_1", "first ")
    rec.text("msg_a1", "prt_2", "second")
    rec.finish(info, "stop")

    assert asyncio.run(src.read()).events[0].text == "first second"


def test_a_tool_call_is_reported_once_however_many_updates_it_takes(rec, workdir):
    src = drain(rec, workdir)
    rec.message("msg_a1", "assistant")
    rec.tool("msg_a1", "prt_tool", "call_1", "read", "contents")

    events = asyncio.run(src.read()).events

    assert [e.kind for e in events] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert all(e.tool_name == "read" for e in events)


def test_a_pending_tool_call_is_not_reported(rec, workdir):
    """Pending carries no arguments yet and may never run."""
    src = drain(rec, workdir)
    rec.message("msg_a1", "assistant")
    rec._part(
        {
            "id": "prt_tool",
            "messageID": "msg_a1",
            "type": "tool",
            "callID": "call_1",
            "tool": "read",
            "state": {"status": "pending", "input": {}},
        }
    )

    assert not asyncio.run(src.read()).events


def test_a_failed_tool_call_still_produces_a_result(rec, workdir):
    src = drain(rec, workdir)
    rec.message("msg_a1", "assistant")
    rec._part(
        {
            "id": "prt_tool",
            "messageID": "msg_a1",
            "type": "tool",
            "callID": "call_1",
            "tool": "bash",
            "state": {"status": "error", "error": "exit 1"},
        }
    )

    events = asyncio.run(src.read()).events

    assert [e.kind for e in events] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert events[1].text == "exit 1"


def test_a_user_part_belonging_to_a_message_we_skipped_is_still_a_user_part(rec, workdir):
    """The role comes from the `message` table when the stream did not say."""
    user = rec.message("msg_u1", "user")
    src = drain(rec, workdir)
    rec.user_text(user["id"], "prt_u1", "late")

    events = asyncio.run(src.read()).events

    assert [(e.kind, e.text) for e in events] == [(EventKind.USER, "late")]


def test_bookkeeping_events_are_progress_without_being_conversation(rec, workdir):
    """Silence here would let the rescue timer fire mid-turn."""
    src = drain(rec, workdir)
    rec.emit("session.updated.1", {"info": {"id": "ses_one"}})

    batch = asyncio.run(src.read())

    assert not batch.events
    assert batch.progressed is True


def test_a_reply_is_stamped_with_when_it_ended(rec, workdir):
    """The first finish has no `time.completed` and the second is deduped, so
    the stamp has to come from the last part the message produced."""
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_1", "done")
    rec.step_finish("msg_a1", "prt_sf", "stop")
    started = info["time"]["created"] / 1000
    rec.finish(info, "stop")

    assert asyncio.run(src.read()).events[0].ts > started


# ---- relocating ---------------------------------------------------------


def test_a_new_session_in_the_same_process_is_picked_up(rec, workdir, tmp_path):
    """A receipt update proves a fresh session still belongs to this process."""
    src, receipt = receipt_source(rec, workdir, tmp_path)
    rec.conn.execute(
        "INSERT INTO session (id, parent_id, directory, time_created) "
        "VALUES ('ses_two', NULL, ?, 9999)",
        (str(workdir.resolve()),),
    )
    rec.conn.commit()
    receipt.write_text(
        json.dumps({"participant_id": "participant", "session_id": "ses_two"}) + "\n"
    )

    batch = asyncio.run(src.refresh())

    assert batch.attached.session_id == "ses_two"
    assert src._session == "ses_one", "the candidate must wait for observer acceptance"
    src.commit_attachment()
    assert src._session == "ses_two"


def test_rejected_new_session_keeps_reading_the_accepted_session(rec, workdir, tmp_path):
    """OpenCode shares one DB, so rejection must preserve its session cursor."""
    src, receipt = receipt_source(rec, workdir, tmp_path)
    rec.conn.execute(
        "INSERT INTO session (id, parent_id, directory, time_created) "
        "VALUES ('ses_foreign', NULL, ?, 9999)",
        (str(workdir.resolve()),),
    )
    rec.conn.commit()
    receipt.write_text(
        json.dumps({"participant_id": "participant", "session_id": "ses_foreign"}) + "\n"
    )

    candidate = asyncio.run(src.refresh())
    assert candidate.attached.session_id == "ses_foreign"
    assert src._session == "ses_one"
    src.discard_attachment()

    user = rec.message("msg_u_after_reject", "user")
    rec.user_text(user["id"], "prt_after_reject", "still mine")
    events = asyncio.run(src.read()).events

    assert [(event.kind, event.text) for event in events] == [(EventKind.USER, "still mine")]


def test_refreshing_onto_the_same_session_reports_nothing(rec, workdir):
    """An empty batch, so the observer's quiet timers keep counting."""
    src = drain(rec, workdir)
    batch = asyncio.run(src.refresh())
    assert batch.attached is None
    assert batch.progressed is False


# ---- history ------------------------------------------------------------


def test_history_rebuilds_the_conversation_from_stored_state(rec, workdir):
    a_turn_with_a_tool(rec)
    src = source_for(rec, workdir)

    history = asyncio.run(src.history(last_n=0))

    assert history.location == "opencode://ses_one"
    assert [e.kind for e in history.events] == [
        EventKind.USER,
        EventKind.TOOL_CALL,
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]
    assert history.events[-1].text == "pamplemousse"
    assert history.events[-1].turn_end is True


def test_history_reads_without_having_polled(rec, workdir):
    """`read_transcript` opens its own source and never calls `read()`."""
    a_turn_with_a_tool(rec)
    src = source_for(rec, workdir, session_id="ses_one")
    assert asyncio.run(src.history(last_n=0)).events


def test_history_does_not_clip(rec, workdir):
    from theater.harness.base import MAX_TEXT

    long = "x" * (MAX_TEXT + 500)
    info = rec.message("msg_a1", "assistant")
    rec.text("msg_a1", "prt_1", long)
    rec.finish(info, "stop")

    history = asyncio.run(source_for(rec, workdir).history(last_n=0))

    assert history.events[-1].text == long


def test_history_returns_the_newest_events_when_asked_for_a_few(rec, workdir):
    a_turn_with_a_tool(rec)
    history = asyncio.run(source_for(rec, workdir).history(last_n=2))
    assert [e.kind for e in history.events] == [
        EventKind.TOOL_RESULT,
        EventKind.ASSISTANT,
    ]


def test_history_of_a_session_that_is_not_there_is_empty(rec, tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    history = asyncio.run(source_for(rec, other).history(last_n=0))
    assert history.location is None
    assert not history.events


def test_the_live_path_and_history_agree(rec, workdir):
    """Two readers of the same session must not tell different stories."""
    src = drain(rec, workdir)
    a_turn_with_a_tool(rec)

    live = asyncio.run(src.read()).events
    stored = asyncio.run(src.history(last_n=0)).events

    assert [(e.kind, e.text, e.turn_end) for e in live] == [
        (e.kind, e.text, e.turn_end) for e in stored
    ]


# ---- the screen ---------------------------------------------------------

#: The footer opencode draws once a conversation exists. The `Ask anything...`
#: placeholder is gone by then, which is why there is no positive marker.
IDLE = "\n".join(["> ", "~/work  90.7K       ctrl+p commands"])
WORKING = "\n".join(["> ", "esc interrupt       ctrl+p commands"])


def test_idle_when_the_footer_is_not_offering_to_interrupt():
    assert OpenCodeObserver().is_idle_screen(IDLE) is True


def test_not_idle_while_a_turn_is_running():
    assert OpenCodeObserver().is_idle_screen(WORKING) is False


def test_a_pane_that_has_not_drawn_yet_is_not_idle():
    """A blank capture is no evidence at all, and must never read as a prompt."""
    assert OpenCodeObserver().is_idle_screen("") is False
    assert OpenCodeObserver().is_idle_screen("loading\n") is False


# ---- how this adapter is observed ---------------------------------------


def test_the_harness_carries_the_observer_the_database_was_given_to():
    """`db` is passed through to the observer, which is the half that opens it.

    Compared by class name rather than `isinstance`: `shipped.py` loads the
    plugin file a second time under its own module name, so the class here is
    not the one the registry holds even though the source is identical.
    """
    observer = OpenCodeHarness(db=Path("/tmp/somewhere.db")).observer
    assert type(observer).__name__ == OpenCodeObserver.__name__
    assert observer.db == Path("/tmp/somewhere.db")
