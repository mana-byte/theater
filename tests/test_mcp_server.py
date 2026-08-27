"""The MCP surface, driven through MCPServer against a real daemon.

No harness and no subprocess: the tools are invoked the same way a client would,
so tool registration, schema generation and the daemon round-trip are all real.
Only the model is missing.
"""

from __future__ import annotations

import json

import pytest

from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.mcp import tools as mcp_tools
from theater.mcp.server import build


@pytest.fixture
async def daemon(theater_home):
    d = Daemon()
    await d.start()
    yield d
    await d.aclose()


def _payload(result):
    """Take the structured half of a CallToolResult.

    A tool annotated `-> dict` structures as that dict; one annotated
    `-> list[dict]` cannot be a JSON object at the root under this protocol
    revision, so the SDK wraps it as {"result": [...]}. Unwrap both.
    """
    structured = result.structured_content
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if structured is not None:
        return structured
    return json.loads(result.content[0].text)


async def test_tools_are_registered(daemon):
    tools = await build("p1", "vibe").list_tools()
    assert {t.name for t in tools} == {
        "whoami",
        "list_participants",
        "list_harnesses",
        "list_models",
        "spawn_session",
        "register_pane",
        "await_sessions",
        "send",
        "scratchpad_write",
        "scratchpad_get",
        "read_transcript",
        "put_child_back_in_the_wound",
        "recall",
        "recall_read",
    }


async def test_every_tool_is_documented(daemon):
    """An agent picks tools by reading descriptions, so blank ones are bugs."""
    for tool in await build("p1", "vibe").list_tools():
        assert tool.description and len(tool.description) > 40, tool.name


async def test_the_server_carries_its_instructions(daemon):
    """The initialize response is the only place the whole loop is described.

    Wiring it is one keyword argument, and losing it would be silent: the
    server still starts, every tool still works, and only the orchestration
    guidance disappears. Nothing else would notice.
    """
    assert build("p1", "vibe").instructions


async def test_orchestration_directives_reach_the_tools_that_need_them(daemon):
    """The directives must live on the tools, not only in `instructions`.

    Clients decide for themselves whether a model ever sees a server's
    instructions, and the harnesses Theater spawns do not agree. A tool
    description is the one channel that always arrives, so each warning is
    asserted where the mistake it prevents is actually made.
    """
    tools = {t.name: t.description or "" for t in await build("p1", "vibe").list_tools()}

    # A "manual" child that nobody is watching never answers its prompt.
    assert "manual" in tools["spawn_session"]
    # An await can outlive the caller's own tool timeout.
    assert "timeout" in tools["await_sessions"]
    # "done" is the end of a turn, not a verdict on the work.
    assert "done" in tools["await_sessions"]
    # Structured JSON transport is a daemon-managed hint, not MCP prompt surgery.
    assert "json.loads" in tools["spawn_session"]
    assert "no schema validation" in tools["spawn_session"]
    assert "json.loads" in tools["send"]
    assert "no schema validation" in tools["send"]


async def test_new_feature_descriptions_reach_their_tools(daemon):
    tools = {t.name: (t.description or "").lower() for t in await build("p1", "vibe").list_tools()}

    for name in ("scratchpad_write", "scratchpad_get"):
        desc = tools[name]
        assert "spawn tree" in desc
        assert "canonical main repo" in desc
        assert "outside a git repository" in desc
        assert "not durable" in desc


async def test_await_description_communicates_first_any_completion(daemon):
    """The await_sessions description must say it returns on the FIRST terminal
    handle, not after all handles finish.

    A model that thinks await waits for every handle will block longer than
    necessary, or never return when one child hangs. The description is the
    only channel that always reaches the model, so the contract — first/any
    completion, re-await the rest — has to be stated there explicitly.
    """
    desc = next(
        t.description or ""
        for t in await build("p1", "vibe").list_tools()
        if t.name == "await_sessions"
    )
    lower = desc.lower()

    # Must communicate first/any completion, not all.
    assert "any" in lower, "description must say it returns when ANY handle is terminal"
    assert "not wait for all" in lower, "description must say it does NOT wait for all handles"

    # Must communicate that already-terminal handles return immediately.
    assert "already" in lower, "description must say already-terminal handles return immediately"

    # Must communicate re-awaiting still-running handles.
    assert "re-await" in lower, "description must say to re-await still-running handles"


async def test_name_semantics_reach_the_tools_that_target_by_name(daemon):
    """Names are live-only, recyclable aliases; the id is stable.

    These directives must live on the tool descriptions, not only in
    `instructions` — a model that never sees the server instructions
    still reads every tool's description. Each tool that accepts a
    participant id or name must warn that names work only while live
    and that a recycled name can identify a successor.
    """
    tools = {t.name: t.description or "" for t in await build("p1", "vibe").list_tools()}

    # list_participants: dead participants have no name; names are recyclable.
    assert "dead" in tools["list_participants"].lower()
    assert "recycl" in tools["list_participants"].lower()

    # send: names work only while live; use id for destructive targeting.
    assert "live" in tools["send"].lower()
    assert "recycl" in tools["send"].lower()

    # read_transcript: names work only while live; dead participants need the id.
    desc = tools["read_transcript"].lower()
    assert "dead" in desc
    assert "id" in desc

    # put_child_back_in_the_wound: already-dead is a no-op only by id;
    # names are recyclable.
    assert "already" in tools["put_child_back_in_the_wound"].lower()
    assert "recycl" in tools["put_child_back_in_the_wound"].lower()


async def test_lazy_session_id_semantics_reach_participant_record_tools(daemon):
    """Every tool returning a participant record explains its nullable id."""
    descriptions = {
        t.name: (t.description or "").lower() for t in await build("p1", "vibe").list_tools()
    }
    for name in ("whoami", "list_participants", "spawn_session", "register_pane"):
        assert "session_id" in descriptions[name]
        assert "null" in descriptions[name]


async def test_spawn_session_forces_a_choice_of_approval(daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    required = schema["spawn_session"]["required"]
    assert "approval" in required
    assert "prompt" not in required
    assert "cwd" not in required


async def test_spawn_session_worktree_schema_accepts_name_bool_or_null(daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    worktree = schema["spawn_session"]["properties"]["worktree"]
    assert {entry["type"] for entry in worktree["anyOf"]} == {"string", "boolean", "null"}


def _assert_nullable_object_schema(prop: dict) -> None:
    assert prop["default"] is None
    variants = prop["anyOf"]
    assert {"type": "null"} in variants
    objects = [variant for variant in variants if variant.get("type") == "object"]
    assert objects
    assert objects[0]["additionalProperties"] is True


async def test_response_format_parameters_have_nullable_object_schema(daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}

    for name in ("spawn_session", "send"):
        prop = schema[name]["properties"]["response_format"]
        _assert_nullable_object_schema(prop)
        assert "response_format" not in schema[name].get("required", [])


async def test_new_tool_schemas_match_public_signatures(daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}

    assert schema["scratchpad_write"]["required"] == ["value", "namespace"]
    assert schema["scratchpad_get"]["required"] == ["namespace"]


async def test_response_format_wrappers_forward_to_tool_bodies(monkeypatch):
    calls = {}

    async def fake_spawn(session, **kwargs):
        calls["spawn"] = (session, kwargs)
        return {"ok": "spawn"}

    async def fake_send(session, **kwargs):
        calls["send"] = (session, kwargs)
        return {"ok": "send"}

    monkeypatch.setattr(mcp_tools, "spawn_session", fake_spawn)
    monkeypatch.setattr(mcp_tools, "send_prompt", fake_send)

    mcp = build("p1", "vibe")
    spawn_format = {}
    send_format = {"type": "object"}

    assert _payload(
        await mcp.call_tool(
            "spawn_session",
            {
                "harness": "vibe",
                "approval": "edits",
                "prompt": "answer in JSON",
                "response_format": spawn_format,
            },
        )
    ) == {"ok": "spawn"}
    assert _payload(
        await mcp.call_tool(
            "send",
            {
                "target_id": "p-child",
                "prompt": "answer in JSON",
                "response_format": send_format,
            },
        )
    ) == {"ok": "send"}

    spawn_session, spawn_kwargs = calls["spawn"]
    send_session, send_kwargs = calls["send"]
    assert isinstance(spawn_session, mcp_tools.Session)
    assert isinstance(send_session, mcp_tools.Session)
    assert spawn_kwargs["response_format"] == spawn_format
    assert send_kwargs["response_format"] == send_format


async def test_new_tool_wrappers_forward_to_tool_bodies(monkeypatch):
    calls = []

    async def fake_scratchpad_write(
        session, *, value: str, namespace: str, key: str | None = None
    ) -> dict:
        calls.append(("scratchpad_write", session, namespace, value, key))
        return {"namespace": namespace, "key": "abc123"}

    async def fake_scratchpad_get(
        session, *, namespace: str, keys: list[str] | None = None
    ) -> dict:
        calls.append(("scratchpad_get", session, namespace, keys))
        return {"namespace": namespace, "entries": {"k1": "v1"}}

    monkeypatch.setattr(mcp_tools, "scratchpad_write", fake_scratchpad_write)
    monkeypatch.setattr(mcp_tools, "scratchpad_get", fake_scratchpad_get)

    mcp = build("p1", "vibe")
    assert _payload(
        await mcp.call_tool(
            "scratchpad_write",
            {"namespace": "plan", "value": "p-you"},
        )
    ) == {"namespace": "plan", "key": "abc123"}
    assert _payload(await mcp.call_tool("scratchpad_get", {"namespace": "plan"})) == {
        "namespace": "plan",
        "entries": {"k1": "v1"},
    }

    assert [(call[0], *call[2:]) for call in calls] == [
        ("scratchpad_write", "plan", "p-you", None),
        ("scratchpad_get", "plan", None),
    ]
    assert all(isinstance(call[1], mcp_tools.Session) for call in calls)


async def test_whoami_registers_on_first_call(daemon):
    mcp = build("chosen-id", "vibe")
    me = _payload(await mcp.call_tool("whoami", {}))

    assert me["id"] == "chosen-id"
    assert me["harness"] == "vibe"
    assert "session_id" in me
    assert me["session_id"] is None
    # No pane reached us: TMUX_PANE is not in the SDK's env allowlist.
    assert me["tier"] == "external"

    async with DaemonClient(autostart=False) as c:
        rows = await c.call("participants.list")
    assert [r["id"] for r in rows] == ["chosen-id"]


async def test_register_pane_promotes_external_to_adopted(daemon):
    mcp = build("chosen-id", "vibe")
    assert _payload(await mcp.call_tool("whoami", {}))["tier"] == "external"

    promoted = _payload(await mcp.call_tool("register_pane", {"pane": "%42"}))
    assert promoted["tier"] == "adopted"
    assert promoted["addressable"] is True
    assert promoted["id"] == "chosen-id"
    assert "session_id" in promoted
    assert promoted["session_id"] is None


async def test_list_participants_marks_the_caller(daemon):
    mine = build("me", "vibe")
    theirs = build("them", "claude")
    await mine.call_tool("whoami", {})
    await theirs.call_tool("whoami", {})

    # The observer normally fills this asynchronously. Updating the raw daemon
    # record gives the MCP boundary a deterministic populated value to expose.
    async with DaemonClient(autostart=False) as c:
        await c.call(
            "hello",
            id="them",
            harness="claude",
            cwd="/tmp",
            session_id="ses-them",
        )

    rows = _payload(await mine.call_tool("list_participants", {}))
    flags = {r["id"]: r["is_self"] for r in rows}
    assert flags == {"me": True, "them": False}
    session_ids = {r["id"]: r["session_id"] for r in rows}
    assert session_ids == {"me": None, "them": "ses-them"}


async def test_a_failing_tool_reports_the_daemon_error(daemon, monkeypatch):
    mcp = build("me", "vibe")
    with pytest.raises(Exception) as exc:
        await mcp.call_tool(
            "spawn_session",
            {"harness": "cursor", "prompt": "hi", "approval": "manual"},
        )
    assert "bad_request" in str(exc.value)


# ---- list_participants: docstring contract ---------------------------------


async def test_list_participants_docstring_describes_resume_state(daemon):
    """The list_participants docstring must describe resume_state and its values."""
    tools = {t.name: t.description or "" for t in await build("p1", "vibe").list_tools()}
    desc = tools["list_participants"].lower()
    assert "resume_state" in desc
    # Each value must be documented.
    for value in (
        "resumable",
        "live",
        "no_session_id",
        "harness_cannot_resume",
        "untrusted",
        "owned_by_live",
    ):
        assert value in desc, f"resume_state value {value!r} missing from docstring"


async def test_list_participants_docstring_describes_ids_semantics(daemon):
    """The list_participants docstring must describe the ids filter and its trap."""
    tools = {t.name: t.description or "" for t in await build("p1", "vibe").list_tools()}
    desc = tools["list_participants"].lower()
    assert "ids" in desc
    # Must warn that unknown ids are silently omitted.
    assert "unknown" in desc or "absent" in desc or "omit" in desc


# ---- list_participants: resume_state pinned to spawner (test 15) ----------
#
# Each case asserts the resume_state value and then calls spawn(resume=...)
# to verify it refuses if and only if the state is not 'resumable'.  The
# harness fixtures are module-level to keep individual tests short.

from pathlib import Path  # noqa: E402  (import after module body — only for fixtures below)

from theater.harness import HARNESSES, Harness, LaunchPlan  # noqa: E402
from theater.harness.contracts.harness import LaunchParameterSupport  # noqa: E402
from theater.harness.observation import TranscriptObserver  # noqa: E402
from theater.protocol import RemoteError  # noqa: E402


class _ResumeObs(TranscriptObserver):
    """Minimal observer for test harnesses."""

    has_transcript = True

    def find_transcript(self, *, cwd, session_id=None, after=None):
        return None

    def session_id(self, transcript):
        return None

    def parse(self, line, index, *, clip_text=True):
        return []

    def is_idle_screen(self, capture):
        return False


class _PinResumeHarness(Harness):
    """Harness that supports resume — used by most resume_state cases."""

    name = "resume-pin-test"
    binary = "resume-pin-test"
    icon = "P"
    launch_parameter_support = LaunchParameterSupport(resume=True)

    def __init__(self):
        self.observer = _ResumeObs()

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        resume: str | None = None,
    ) -> LaunchPlan:
        argv = ["resume-pin-test"]
        if resume:
            argv += ["--resume", resume]
        return LaunchPlan(argv=argv)


class _PinNoResumeHarness(Harness):
    """Harness whose plan_launch predates the resume parameter."""

    name = "no-resume-pin-test"
    binary = "no-resume-pin-test"
    icon = "Q"

    def __init__(self):
        self.observer = _ResumeObs()

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
    ) -> LaunchPlan:
        return LaunchPlan(argv=["no-resume-pin-test"])


@pytest.fixture
def pin_harnesses(monkeypatch):
    """Install the two test harnesses and stub shutil.which."""
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setitem(HARNESSES, "resume-pin-test", _PinResumeHarness())
    monkeypatch.setitem(HARNESSES, "no-resume-pin-test", _PinNoResumeHarness())


async def test_resume_state_live_spawn_refuses(daemon, pin_harnesses):
    """live => spawn(resume=id) refuses with _resolve_resume_reference's 'still live' message."""
    async with DaemonClient(autostart=False) as c:
        me = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        pid = me["id"]
        rows = await c.call("participants.list")
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "live"
        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume=pid,
            )
        assert exc.value.code == "bad_request"
        assert "still live" in exc.value.message


async def test_resume_state_no_session_id_spawn_refuses(daemon, pin_harnesses):
    """no_session_id => spawn(resume=id) refuses with 'not recorded' message."""
    async with DaemonClient(autostart=False) as c:
        me = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        pid = me["id"]
        daemon.registry.mark_dead(pid)
        rows = await c.call("participants.list", include_dead=True)
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "no_session_id"
        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume=pid,
            )
        assert exc.value.code == "bad_request"
        assert "harness session id" in exc.value.message


async def test_resume_state_harness_cannot_resume_spawn_refuses(daemon, pin_harnesses):
    """harness_cannot_resume => spawn(resume=session_id) refuses with 'does not support' message."""
    async with DaemonClient(autostart=False) as c:
        p = await c.call("hello", harness="no-resume-pin-test", cwd="/tmp")
        pid = p["id"]
        part = daemon.registry.get(pid)
        part.session_id = "sess-noresume"
        part.session_correlation = "operator"
        daemon.store.upsert_participant(part)
        daemon.registry.mark_dead(pid)
        rows = await c.call("participants.list", include_dead=True)
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "harness_cannot_resume"
        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="no-resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume="sess-noresume",
            )
        assert exc.value.code == "bad_request"
        assert "does not support resume" in exc.value.message


async def test_resume_state_untrusted_spawn_refuses(daemon, pin_harnesses):
    """untrusted => spawn(resume=session_id) refuses with 'no trusted ... binding' message."""
    async with DaemonClient(autostart=False) as c:
        p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        pid = p["id"]
        part = daemon.registry.get(pid)
        part.session_id = "sess-untrusted"
        part.session_correlation = "heuristic"
        daemon.store.upsert_participant(part)
        daemon.registry.mark_dead(pid)
        rows = await c.call("participants.list", include_dead=True)
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "untrusted"
        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume="sess-untrusted",
            )
        assert exc.value.code == "bad_request"
        # The spawner reaches the "no trusted dead binding" gate, not the live-owner gate.
        assert "no trusted" in exc.value.message


async def test_resume_state_resumable_spawn_succeeds(daemon, pin_harnesses):
    """resumable => spawn(resume=session_id) succeeds (no refusal)."""
    async with DaemonClient(autostart=False) as c:
        p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        pid = p["id"]
        part = daemon.registry.get(pid)
        part.session_id = "sess-resumable"
        part.session_correlation = "operator"
        daemon.store.upsert_participant(part)
        daemon.registry.mark_dead(pid)
        rows = await c.call("participants.list", include_dead=True)
        row = next(r for r in rows if r["id"] == pid)
        assert row["resume_state"] == "resumable"
        result = await c.call(
            "spawn",
            harness="resume-pin-test",
            prompt="",
            approval="manual",
            cwd="/tmp",
            resume="sess-resumable",
        )
        assert result["id"] is not None


async def test_resume_state_owned_by_live_trusted_dead_spawn_refuses(daemon, pin_harnesses):
    """owned_by_live, trusted dead + trusted live => 'trusted owner is still live' message."""
    async with DaemonClient(autostart=False) as c:
        # Live owner with trusted binding.
        live_p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        lo_id = live_p["id"]
        live_part = daemon.registry.get(lo_id)
        live_part.session_id = "sess-owned"
        live_part.session_correlation = "operator"
        daemon.store.upsert_participant(live_part)

        # Dead predecessor sharing the same session id (trusted provenance).
        dead_p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        dp_id = dead_p["id"]
        dead_part = daemon.registry.get(dp_id)
        dead_part.session_id = "sess-owned"
        dead_part.session_correlation = "operator"
        daemon.store.upsert_participant(dead_part)
        daemon.registry.mark_dead(dp_id)

        rows = await c.call("participants.list", include_dead=True)
        dp_row = next(r for r in rows if r["id"] == dp_id)
        assert dp_row["resume_state"] == "owned_by_live"

        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume="sess-owned",
            )
        assert exc.value.code == "bad_request"
        assert "still live" in exc.value.message
        assert lo_id in exc.value.message


async def test_resume_state_owned_by_live_untrusted_dead_spawn_refuses(daemon, pin_harnesses):
    """owned_by_live wins even when the subject dead row is untrusted.

    An untrusted dead row and a trusted live peer sharing a session id: the
    spawner's _validate_resume_identity filters to trusted rows only, so the
    untrusted dead row enters neither list and the live trusted peer triggers
    the 'trusted owner is still live' gate.  resume_state must report
    owned_by_live (not untrusted), and the spawner must refuse with the
    'still live' message — proving the two implementations agree.
    """
    async with DaemonClient(autostart=False) as c:
        # Live owner with trusted binding.
        live_p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        lo_id = live_p["id"]
        live_part = daemon.registry.get(lo_id)
        live_part.session_id = "sess-mixed"
        live_part.session_correlation = "operator"
        daemon.store.upsert_participant(live_part)

        # Dead row sharing the session id but with UNTRUSTED provenance.
        dead_p = await c.call("hello", harness="resume-pin-test", cwd="/tmp")
        dp_id = dead_p["id"]
        dead_part = daemon.registry.get(dp_id)
        dead_part.session_id = "sess-mixed"
        dead_part.session_correlation = "heuristic"  # untrusted
        daemon.store.upsert_participant(dead_part)
        daemon.registry.mark_dead(dp_id)

        rows = await c.call("participants.list", include_dead=True)
        dp_row = next(r for r in rows if r["id"] == dp_id)
        # Must be owned_by_live, not untrusted — the live peer dominates.
        assert dp_row["resume_state"] == "owned_by_live"

        with pytest.raises(RemoteError) as exc:
            await c.call(
                "spawn",
                harness="resume-pin-test",
                prompt="",
                approval="manual",
                cwd="/tmp",
                resume="sess-mixed",
            )
        assert exc.value.code == "bad_request"
        # Must hit the live-owner gate, not the untrusted gate.
        assert "still live" in exc.value.message
        assert lo_id in exc.value.message
