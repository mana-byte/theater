"""OpenCode 2.x: launch wiring, lineage resume, and the storage projection the observer reads.

`RecorderV2` writes the two 2.x tables the way the server projects them: one row per message
with its parts embedded, rows rewritten in place (so `time_updated` moves), and one `idle` row
closing every turn — the shapes were taken from a live 2.0.18 `opencode.db`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from shipped import OpenCodeHarness

from theater.harness import EventKind, TurnTerminal, theater_mcp_servers
from theater.harness.base import EventPath
from theater.harness.builtin.plugins.opencode.dialect import (
    OpenCodeDialect,
    domain_for,
    v2_database_path,
)
from theater.harness.builtin.plugins.opencode.native_plugin_v2 import (
    plugin_dir,
    render_native_plugin_v2,
)
from theater.harness.builtin.plugins.opencode.runtime_plan import parse_opencode_version
from theater.harness.builtin.plugins.opencode.source_v2 import OpenCodeV2Source
from theater.harness.contracts.context import ParticipantObservationContext
from theater.models import BadRequest, Participant, Status

V2 = OpenCodeDialect.V2

SCHEMA = """
CREATE TABLE session_v2 (
    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT NOT NULL, time_created INTEGER NOT NULL
);
CREATE TABLE session_message (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL
);
"""


class RecorderV2:
    def __init__(self, path: Path, sid: str, directory: str, created: int = 1000):
        self.path = path
        self.sid = sid
        self.clock = created
        self.seq = 0
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.execute(
            "INSERT INTO session_v2 VALUES (?, NULL, ?, ?)",
            (sid, str(Path(directory).resolve()), created),
        )
        self.conn.commit()

    def tick(self, ms: int = 10) -> int:
        self.clock += ms
        return self.clock

    def write(self, mid: str, kind: str, data: dict, *, at: int | None = None) -> None:
        """Insert, or rewrite in place keeping the row's `seq`, as the projector does."""
        at = self.tick() if at is None else at
        self.seq += 1
        self.conn.execute(
            "INSERT INTO session_message VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE "
            "SET data = excluded.data, time_updated = excluded.time_updated",
            (mid, self.sid, kind, self.seq, data["time"]["created"], at, json.dumps(data)),
        )
        self.conn.commit()

    def user(self, mid: str, text: str) -> None:
        self.write(mid, "user", {"time": {"created": self.tick()}, "text": text, "files": []})

    def step(self, mid: str, created: int, content: list, *, at: int | None = None, **fields):
        data = {
            "time": {"created": created, **fields.pop("times", {})},
            "agent": "build",
            "model": {"id": "claude", "providerID": "anthropic"},
            "content": content,
            **fields,
        }
        self.write(mid, "assistant", data, at=at)

    def idle(self, mid: str, outcome: str) -> None:
        self.write(mid, "idle", {"time": {"created": self.tick()}, "outcome": outcome})


def tool(call: str, status: str, path: str, output: str = "") -> dict:
    state: dict = {"status": status, "input": {"path": path}}
    if status == "completed":
        state["content"] = [{"type": "text", "text": output}]
    return {"type": "tool", "id": call, "name": "read", "state": state, "time": {"created": 1}}


def text(body: str) -> dict:
    return {"type": "text", "text": body}


USAGE = {"tokens": {"input": 3, "output": 5, "reasoning": 0, "cache": {"read": 0, "write": 0}}}


@pytest.fixture
def workdir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def rec(tmp_path, workdir):
    r = RecorderV2(tmp_path / "opencode-v2.db", "ses_one", str(workdir))
    yield r
    r.conn.close()


def attached(rec, workdir) -> OpenCodeV2Source:
    src = OpenCodeV2Source(rec.path, cwd=str(workdir))
    batch = asyncio.run(src.read())
    assert batch.attached is not None
    src.commit_attachment()
    return src


def drain(src):
    """The conversation a read reports; usage-only step accounting is not part of it."""
    return [e for e in asyncio.run(src.read()).events if not e.usage_only]


def a_turn_with_a_tool(rec, workdir) -> None:
    note = str(workdir / "note.txt")
    rec.user("msg_u1", "read the note")
    first = rec.tick()
    rec.step("msg_a1", first, [tool("call_1", "running", note)])
    rec.step(
        "msg_a1",
        first,
        [tool("call_1", "completed", note, "the secret is pamplemousse")],
        finish="tool-calls",
        times={"completed": rec.tick()},
        **USAGE,
    )
    second = rec.tick()
    rec.step("msg_a2", second, [text("")])
    rec.step(
        "msg_a2",
        second,
        [text("pamplemousse")],
        finish="stop",
        times={"completed": rec.tick()},
        **USAGE,
    )
    rec.idle("msg_i1", "succeeded")


# ---- the release and the launch ----------------------------------------


def test_the_2x_version_banner_names_its_release():
    assert parse_opencode_version("opencode v2.0.18\n") == (2, 0, 18)
    assert parse_opencode_version("1.18.29") == (1, 18, 29)


def test_a_2x_launch_runs_a_private_server_on_its_own_database(tmp_path):
    config = tmp_path / "abc.json"
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=config,
        approval="manual",
        model="anthropic/claude",
        mcp_servers=theater_mcp_servers("abc123", "opencode"),
    )

    database = v2_database_path("abc123")
    assert plan.argv[-6:-2] == ["opencode", "--standalone", "--prompt", "say hello"]
    assert plan.env == {
        "OPENCODE_CONFIG": str(config),
        "OPENCODE_DB": str(database),
        # A `cli.json` autoaccept would otherwise answer the plugin's asks in the client.
        "OPENCODE_CLI_CONFIG_CONTENT": '{"session": {"permissions": "prompt"}}',
    }
    assert plan.transcript_domain == domain_for(database)
    assert database.parent / ".opencode-v2" in plan.files
    document = json.loads(plan.files[config])
    assert document["plugin"] == [plugin_dir(config).resolve().as_uri()]
    assert document["model"] == "anthropic/claude"
    theater = document["mcp"]["servers"]["theater"]
    assert theater["type"] == "local" and theater["codemode"] is False
    assert "enabled" not in theater
    package = json.loads(plan.files[plugin_dir(config) / "package.json"])
    assert package["type"] == "module"
    assert "export default" in plan.files[plugin_dir(config) / "server.js"]


def test_a_new_session_exists_before_its_tui_opens_on_it(tmp_path):
    """2.0.18 drops a cold home screen's `--prompt`; a session's screen submits it once ready."""
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="say hello",
        config_path=tmp_path / "abc.json",
        approval="manual",
    )
    calls = tmp_path / "calls"
    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {calls}\n')
    fake.chmod(0o755)
    path = f"{fake.parent}:{os.environ['PATH']}"
    subprocess.run(plan.argv, env={**os.environ, "PATH": path}, check=True, timeout=30)

    created, opened = calls.read_text().splitlines()
    session = json.loads(created.removeprefix("api --standalone POST /api/session -d "))["id"]
    assert session.startswith("ses_")
    assert opened == f"--standalone --prompt say hello -s {session}"


def test_yolo_auto_accepts_and_a_resume_continues_its_known_session(tmp_path):
    config = tmp_path / "abc.json"
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt="",
        config_path=config,
        approval="yolo",
        resume="ses_1",
    )
    assert plan.argv == ["opencode", "--standalone", "--auto", "-s", "ses_1"]
    assert "OPENCODE_CLI_CONFIG_CONTENT" not in plan.env
    assert plan.session_id == "ses_1"
    assert '"rules": []' in plan.files[plugin_dir(config) / "server.js"]


def test_a_resume_stays_in_its_lineage_and_never_crosses_releases(tmp_path):
    lineage = v2_database_path("first")
    lineage.parent.mkdir(parents=True)
    lineage.touch()
    predecessor = Participant(id="second", transcript_domain=domain_for(lineage))

    overlay = OpenCodeHarness(dialect=V2).resume_launch_overlay(
        predecessor=predecessor, trusted_session_owners=(predecessor,)
    )
    assert overlay.env == {"OPENCODE_DB": str(lineage.resolve())}
    assert overlay.transcript_domain == domain_for(lineage)

    with pytest.raises(BadRequest, match="not a Theater-isolated"):
        OpenCodeHarness(dialect=V2).resume_launch_overlay(
            predecessor=predecessor, trusted_session_owners=()
        )
    v1_domain = f"opencode://{(tmp_path / 'opencode.db').resolve()}"
    with pytest.raises(BadRequest, match=r"recorded by OpenCode 1\.x"):
        OpenCodeHarness(dialect=V2).resume_launch_overlay(
            predecessor=Participant(id="old", transcript_domain=v1_domain),
            trusted_session_owners=(),
        )
    with pytest.raises(BadRequest, match=r"recorded by OpenCode 2\.x"):
        OpenCodeHarness(db=tmp_path / "opencode.db").resume_launch_overlay(
            predecessor=predecessor, trusted_session_owners=(predecessor,)
        )


# ---- observing ----------------------------------------------------------


def test_a_2x_domain_opens_its_own_lineage_database(rec, workdir):
    context = ParticipantObservationContext(
        participant_id="abc123", cwd=str(workdir), transcript_domain=domain_for(rec.path)
    )
    src = OpenCodeHarness().observer.open_source_context(context)
    assert src.collision_domain == domain_for(rec.path)
    batch = asyncio.run(src.read())
    assert batch.attached is not None and batch.attached.session_id == "ses_one"


def test_a_turn_reads_as_one_user_one_tool_pair_and_one_reply(rec, workdir):
    src = attached(rec, workdir)
    a_turn_with_a_tool(rec, workdir)
    events = drain(src)

    assert [(e.kind, e.tool_name) for e in events] == [
        (EventKind.USER, None),
        (EventKind.TOOL_CALL, "read"),
        (EventKind.TOOL_RESULT, "read"),
        (EventKind.ASSISTANT, None),
    ]
    assert events[0].text == "read the note"
    assert events[1].paths == (EventPath(path="note.txt", mode="read"),)
    assert events[2].text == "the secret is pamplemousse"
    assert events[3].text == "pamplemousse"
    assert events[3].turn_terminal is TurnTerminal.COMPLETED
    assert events[3].usage is not None and events[3].usage.model == "anthropic/claude"
    assert drain(src) == []


def test_status_follows_the_idle_marker(rec, workdir):
    rec.user("msg_u1", "hello")
    assert asyncio.run(OpenCodeV2Source(rec.path, cwd=str(workdir)).read()).status is (
        Status.WORKING
    )
    rec.idle("msg_i1", "succeeded")
    assert asyncio.run(OpenCodeV2Source(rec.path, cwd=str(workdir)).read()).status is Status.IDLE


def test_an_interrupt_between_steps_still_ends_the_turn(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "read the note")
    first = rec.tick()
    rec.step(
        "msg_a1",
        first,
        [tool("call_1", "completed", "/elsewhere", "done")],
        finish="tool-calls",
        times={"completed": rec.tick()},
    )
    rec.idle("msg_i1", "interrupted")
    events = drain(src)

    assert events[-1].kind is EventKind.ERROR
    assert events[-1].turn_terminal is TurnTerminal.INTERRUPTED
    assert sum(e.turn_end for e in events) == 1


def test_an_aborted_step_is_an_interrupt(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "go")
    rec.step(
        "msg_a1",
        rec.tick(),
        [text("partial")],
        finish="error",
        error={"type": "aborted", "message": "Step interrupted"},
        times={"completed": rec.tick()},
    )
    rec.idle("msg_i1", "interrupted")
    ends = [e for e in drain(src) if e.turn_end]
    assert [(e.kind, e.turn_terminal) for e in ends] == [
        (EventKind.ERROR, TurnTerminal.INTERRUPTED)
    ]


def test_history_and_the_live_path_agree(rec, workdir):
    src = attached(rec, workdir)
    a_turn_with_a_tool(rec, workdir)
    live = [(e.kind, e.text, e.turn_end) for e in drain(src)]
    history = asyncio.run(src.history(last_n=0)).events
    assert [(e.kind, e.text, e.turn_end) for e in history] == live


def test_attaching_mid_turn_reports_only_what_happens_after(rec, workdir):
    note = str(workdir / "note.txt")
    rec.user("msg_u1", "read both")
    first = rec.tick()
    rec.step(
        "msg_a1",
        first,
        [tool("call_1", "completed", note, "one"), tool("call_2", "running", note)],
    )
    src = attached(rec, workdir)
    rec.step(
        "msg_a1",
        first,
        [tool("call_1", "completed", note, "one"), tool("call_2", "completed", note, "two")],
        finish="tool-calls",
        times={"completed": rec.tick()},
    )
    assert [(e.kind, e.text) for e in drain(src)] == [(EventKind.TOOL_RESULT, "two")]


def test_a_rewrite_in_the_same_millisecond_is_still_read(rec, workdir):
    src = attached(rec, workdir)
    rec.user("msg_u1", "hi")
    created = rec.tick()
    at = rec.tick()
    rec.step("msg_a1", created, [text("hello")], at=at)
    assert all(not e.turn_end for e in drain(src))
    rec.step("msg_a1", created, [text("hello")], at=at, finish="stop", times={"completed": at})
    assert [e.turn_end for e in drain(src)] == [True]


# ---- the plugin ---------------------------------------------------------

_PLUGIN_PROBE = """
const plugin = (await import(process.argv[1])).default
const hooks = {}
const registered = (name) => async (hook, callback) => {
  hooks[`${name}.${hook}`] = callback
  return { dispose: async () => {} }
}
const never = { [Symbol.asyncIterator]: () => ({ next: () => new Promise(() => {}) }) }
await plugin.setup({
  location: { directory: "/w" },
  permission: { hook: registered("permission") },
  session: { hook: registered("session"), get: async () => ({ id: "ses_1" }) },
  tool: { hook: registered("tool") },
  mcp: { list: async () => ({ data: [] }) },
  event: { subscribe: () => never },
})
const decide = (action, resources, effect = "allow") => {
  const event = { action, resources, effect }
  hooks["permission.evaluate"]?.(event)
  return event.effect
}
console.log(JSON.stringify({
  id: plugin.id,
  shell: decide("shell", ["ls"]),
  read: decide("read", ["/w/a.txt"]),
  env: decide("read", ["/w/.env"]),
  example: decide("read", ["/w/.env.example"]),
  edit: decide("edit", ["/w/a.txt"]),
  model: decide("provider.use", ["anthropic/claude"]),
  nativeAsk: decide("read", ["/w/a.txt"], "ask"),
}))
process.exit(0)
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the plugin")
@pytest.mark.parametrize(
    ("approval", "edit", "shell"),
    [("manual", "ask", "ask"), ("edits", "allow", "ask"), ("yolo", "allow", "allow")],
)
def test_the_plugin_only_tightens_native_decisions(tmp_path, approval, edit, shell):
    files = render_native_plugin_v2("abc123", tmp_path / "x.json", tmp_path / "token", approval)
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    server = plugin_dir(tmp_path / "x.json") / "server.js"
    run = subprocess.run(
        ["node", "--input-type=module", "-e", _PLUGIN_PROBE, server.as_uri()],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    decided = json.loads(run.stdout)
    assert decided["id"] == "theater.opencode-session"
    assert (decided["edit"], decided["shell"]) == (edit, shell)
    assert decided["read"] == "allow" and decided["model"] == "allow"
    assert decided["nativeAsk"] == "ask"
    if approval != "yolo":
        assert (decided["env"], decided["example"]) == ("ask", "allow")
