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
        "store_put",
        "store_get",
        "checkpoint",
        "list_checkpoints",
        "recovery_read",
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

    for name in ("store_put", "store_get"):
        desc = tools[name]
        assert "exact string" in desc
        assert "last-writer-wins" in desc
        assert "spawn tree" in desc
        assert "canonical main repo" in desc
        assert "outside a git repository" in desc
        assert "prefix listing" in desc
        assert "cas" not in desc
        assert "lock" not in desc

    assert "explicit" in tools["checkpoint"]
    assert "cumulative snapshot" in tools["checkpoint"]
    assert "not an automatic execution checkpoint" in tools["checkpoint"]

    assert "recorded snapshot" in tools["recovery_read"]
    assert "current live state" in tools["recovery_read"]
    assert "pruned handles" in tools["recovery_read"]


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

    assert schema["store_put"]["required"] == ["namespace", "key", "value"]
    assert schema["store_get"]["required"] == ["namespace", "key"]
    assert schema["checkpoint"]["required"] == ["name"]
    assert schema["recovery_read"]["required"] == ["checkpoint_id"]

    notes = schema["checkpoint"]["properties"]["notes"]
    variants = notes["anyOf"]
    assert {"type": "string"} in variants
    assert {"type": "null"} in variants
    assert notes["default"] is None


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

    async def fake_store_put(session, *, namespace: str, key: str, value: str) -> dict:
        calls.append(("store_put", session, namespace, key, value))
        return {"stored": True}

    async def fake_store_get(session, *, namespace: str, key: str) -> dict:
        calls.append(("store_get", session, namespace, key))
        return {"value": "p-you"}

    async def fake_checkpoint(session, *, name: str, notes: str | None = None) -> dict:
        calls.append(("checkpoint", session, name, notes))
        return {"checkpoint_id": 7, "jobs": []}

    async def fake_recovery_read(session, *, checkpoint_id: int) -> dict:
        calls.append(("recovery_read", session, checkpoint_id))
        return {"checkpoint_id": checkpoint_id, "recorded": [], "live": []}

    monkeypatch.setattr(mcp_tools, "store_put", fake_store_put)
    monkeypatch.setattr(mcp_tools, "store_get", fake_store_get)
    monkeypatch.setattr(mcp_tools, "checkpoint", fake_checkpoint)
    monkeypatch.setattr(mcp_tools, "recovery_read", fake_recovery_read)

    mcp = build("p1", "vibe")
    assert _payload(
        await mcp.call_tool(
            "store_put",
            {"namespace": "plan", "key": "owner", "value": "p-you"},
        )
    ) == {"stored": True}
    assert _payload(await mcp.call_tool("store_get", {"namespace": "plan", "key": "owner"})) == {
        "value": "p-you"
    }
    assert _payload(
        await mcp.call_tool("checkpoint", {"name": "before merge", "notes": "watch p-you"})
    ) == {"checkpoint_id": 7, "jobs": []}
    assert _payload(await mcp.call_tool("recovery_read", {"checkpoint_id": 7})) == {
        "checkpoint_id": 7,
        "recorded": [],
        "live": [],
    }

    assert [(call[0], *call[2:]) for call in calls] == [
        ("store_put", "plan", "owner", "p-you"),
        ("store_get", "plan", "owner"),
        ("checkpoint", "before merge", "watch p-you"),
        ("recovery_read", 7),
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
