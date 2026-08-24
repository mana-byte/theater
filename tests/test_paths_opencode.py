"""Path capture in the opencode adapter: Event.paths extraction.

Tests the recall substrate (docs/v2_recall.md, piece 1) against the real
fixture shapes and the real opencode tool input dialect. opencode stores
its tool-call arguments in ``state.input`` as a JSON object whose keys are
the parameter schema field names — ``filePath`` for write/edit/read,
``path`` (a directory) for glob/grep, and no path at all for bash/shell.

The Recorder here extends the one in ``test_harness_opencode.py`` with a
``tool_with_input`` method that writes a tool part whose ``state.input``
matches the real shape, so the path extraction runs against the same
record structure the adapter sees in production.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from shipped import OpenCodeHarness, OpenCodeObserver

from theater.harness import EventKind
from theater.harness.base import EventPath
from theater.harness.builtin.plugins.opencode import _paths_from_tool, _relativise

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
    """Same shape as the one in test_harness_opencode.py, with a tool method
    that accepts the real ``state.input`` structure."""

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

    def message(self, mid: str, role: str) -> dict:
        info = {"id": mid, "role": role, "time": {"created": self.tick()}}
        self.emit("message.updated.1", {"info": info})
        self._store_message(mid, info)
        return info

    def finish(self, info: dict, reason: str) -> None:
        info = dict(info, finish=reason)
        self.emit("message.updated.1", {"info": info})
        info = dict(info, time=dict(info["time"], completed=self.tick()))
        self.emit("message.updated.1", {"info": info})
        self._store_message(info["id"], info)

    def step_finish(self, mid: str, pid: str, reason: str) -> None:
        self._part({"id": pid, "messageID": mid, "type": "step-finish", "reason": reason})

    def tool_with_input(
        self,
        mid: str,
        pid: str,
        call: str,
        name: str,
        input_data: dict,
        output: str = "",
    ) -> None:
        """A tool part with the real ``state.input`` shape.

        ``input_data`` is exactly what the LLM passed: ``{"filePath": ...}``
        for write/edit/read, etc. It lands in ``state.input`` at every
        status from ``running`` onwards, matching the real schema
        (session.ts:266-289): ``running`` and ``completed`` both carry it.
        The ``part`` table keeps only the last write per part id, so the
        ``completed`` state must include ``input`` for history to see it.
        """
        base = {
            "id": pid,
            "messageID": mid,
            "type": "tool",
            "callID": call,
            "tool": name,
        }
        self._part({**base, "state": {"status": "pending", "input": {}}})
        self._part({**base, "state": {"status": "running", "input": input_data}})
        self._part(
            {
                **base,
                "state": {"status": "completed", "input": input_data, "output": output},
            }
        )


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


def drain(rec, workdir):
    """Attach before anything happened, then read the whole session live."""
    src = source_for(rec, workdir)
    asyncio.run(src.read())
    src.commit_attachment()
    return src


def _tool_call_events(events: list) -> list:
    return [e for e in events if e.kind is EventKind.TOOL_CALL]


# ---- unit: _paths_from_tool ---------------------------------------------


def test_write_tool_input_yields_write_path():
    state = {"input": {"filePath": "src/app.py", "content": "x = 1"}}
    assert _paths_from_tool("write", state, "/repo") == (
        EventPath(path="src/app.py", mode="write"),
    )


def test_edit_tool_input_yields_write_path():
    state = {
        "input": {"filePath": "src/app.py", "oldString": "a", "newString": "b"},
    }
    assert _paths_from_tool("edit", state, "/repo") == (EventPath(path="src/app.py", mode="write"),)


def test_read_tool_input_yields_read_path():
    state = {"input": {"filePath": "README.md", "offset": 1, "limit": 100}}
    assert _paths_from_tool("read", state, "/repo") == (EventPath(path="README.md", mode="read"),)


def test_webfetch_yields_no_paths():
    state = {"input": {"url": "https://example.com", "format": "markdown"}}
    assert _paths_from_tool("webfetch", state, "/repo") == ()


def test_pending_state_yields_no_paths():
    """Pending carries no input yet."""
    assert _paths_from_tool("write", {"status": "pending", "input": {}}, "/repo") == ()


def test_missing_filepath_yields_no_paths():
    state = {"input": {"content": "x"}}
    assert _paths_from_tool("write", state, "/repo") == ()


def test_non_string_filepath_yields_no_paths():
    state = {"input": {"filePath": 42}}
    assert _paths_from_tool("write", state, "/repo") == ()


# ---- unit: _relativise ---------------------------------------------------


def test_relativise_absolute_path_inside_repo():
    assert _relativise("/repo/src/app.py", "/repo") == "src/app.py"


def test_relativise_relative_path_unchanged():
    assert _relativise("src/app.py", "/repo") == "src/app.py"


def test_relativise_absolute_path_outside_repo_is_none():
    assert _relativise("/other/src/app.py", "/repo") is None


def test_relativise_absolute_path_without_cwd_is_none():
    """An absolute path with no cwd to relativise against would leak a home
    directory into the index, so it is dropped."""
    assert _relativise("/Users/manaiki.laut/repo/src/app.py", None) is None


def test_relativise_empty_is_none():
    assert _relativise("", "/repo") is None


# ---- integration: live path (read) ---------------------------------------


def test_a_write_tool_call_attaches_a_write_path(rec, workdir):
    """An edit tool call produces a TOOL_CALL event with a write EventPath."""
    src = drain(rec, workdir)
    info = rec.message("msg_a1", "assistant")
    rec.tool_with_input(
        "msg_a1",
        "prt_edit",
        "call_1",
        "edit",
        {"filePath": "src/app.py", "oldString": "a", "newString": "b"},
        output="ok",
    )
    rec.step_finish("msg_a1", "prt_sf", "tool-calls")
    rec.finish(info, "tool-calls")

    events = asyncio.run(src.read()).events
    calls = _tool_call_events(events)
    assert len(calls) == 1
    assert calls[0].paths == (EventPath(path="src/app.py", mode="write"),)


def test_a_read_tool_call_attaches_a_read_path(rec, workdir):
    """A read tool call produces a TOOL_CALL event with a read EventPath."""
    src = drain(rec, workdir)
    rec.message("msg_a1", "assistant")
    rec.tool_with_input(
        "msg_a1",
        "prt_read",
        "call_1",
        "read",
        {"filePath": "README.md"},
        output="contents",
    )

    events = asyncio.run(src.read()).events
    calls = _tool_call_events(events)
    assert len(calls) == 1
    assert calls[0].paths == (EventPath(path="README.md", mode="read"),)


def test_an_event_with_no_paths_is_empty(rec, workdir):
    """A session.updated event yields no events at all."""
    src = drain(rec, workdir)
    rec.emit("session.updated.1", {"info": {"id": "ses_one"}})

    batch = asyncio.run(src.read())
    assert not batch.events


def test_two_tool_calls_in_one_message_each_carry_paths(rec, workdir):
    """The closest opencode gets to a multi-file event: two tool calls in
    one message, each touching a different file."""
    src = drain(rec, workdir)
    rec.message("msg_a1", "assistant")
    rec.tool_with_input(
        "msg_a1",
        "prt_write",
        "call_1",
        "write",
        {"filePath": "src/new.py", "content": "x = 1"},
        output="ok",
    )
    rec.tool_with_input(
        "msg_a1",
        "prt_read",
        "call_2",
        "read",
        {"filePath": "src/old.py"},
        output="y = 2",
    )

    events = asyncio.run(src.read()).events
    calls = _tool_call_events(events)
    assert len(calls) == 2
    assert calls[0].paths == (EventPath(path="src/new.py", mode="write"),)
    assert calls[1].paths == (EventPath(path="src/old.py", mode="read"),)


# ---- integration: history path ------------------------------------------


def test_history_also_carries_paths(rec, workdir):
    """The history path (``_replay``) attaches paths the same way the live
    path does, so ``read_transcript`` returns them too."""
    info = rec.message("msg_a1", "assistant")
    rec.tool_with_input(
        "msg_a1",
        "prt_edit",
        "call_1",
        "edit",
        {"filePath": "src/app.py", "oldString": "a", "newString": "b"},
        output="ok",
    )
    rec.finish(info, "stop")

    history = asyncio.run(source_for(rec, workdir).history(last_n=0))
    calls = _tool_call_events(list(history.events))
    assert len(calls) == 1
    assert calls[0].paths == (EventPath(path="src/app.py", mode="write"),)


# ---- resume -------------------------------------------------------------


def test_resume_forks_the_session_and_waits_for_its_receipt(tmp_path):
    plan = OpenCodeHarness().plan_launch(
        participant_id="abc123",
        prompt="do something",
        config_path=tmp_path / "x.json",
        approval="manual",
        resume="ses_ffb42302cffeaasiFBDGgLmkRf",
    )
    assert plan.argv == [
        "opencode",
        "-s",
        "ses_ffb42302cffeaasiFBDGgLmkRf",
        "--fork",
    ]
    receipt = tmp_path / "x.opencode-session"
    assert receipt not in plan.files
    assert plan.session_id is None


def test_resume_with_model_and_auto(tmp_path):
    """Model and approval flags still apply when resuming."""
    plan = OpenCodeHarness().plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=tmp_path / "x.json",
        approval="yolo",
        model="openai-foundry/zai-glm-5-2",
        resume="ses_abc",
    )
    assert plan.argv == [
        "opencode",
        "--model",
        "openai-foundry/zai-glm-5-2",
        "--auto",
        "-s",
        "ses_abc",
        "--fork",
    ]


def test_resume_takes_prompt_is_false():
    """opencode drops the prompt when resuming: `-s` routes to the session
    view and `--prompt` is only read on the home screen."""
    assert OpenCodeHarness.resume_takes_prompt is False
