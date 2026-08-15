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

    # Concurrent children whose files overlap produce a conflict, not speed.
    assert "sibling" in tools["spawn_session"]
    # A "manual" child that nobody is watching never answers its prompt.
    assert "manual" in tools["spawn_session"]
    # An await can outlive the caller's own tool timeout.
    assert "timeout" in tools["await_sessions"]
    # "done" is the end of a turn, not a verdict on the work.
    assert "done" in tools["await_sessions"]
    # Killing a worktree child deletes the branch its work lives on.
    assert "collect before you kill" in tools["put_child_back_in_the_wound"]


async def test_spawn_session_forces_a_choice_of_approval(daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    required = schema["spawn_session"]["required"]
    assert "approval" in required
    assert "prompt" not in required
    assert "cwd" not in required


async def test_whoami_registers_on_first_call(daemon):
    mcp = build("chosen-id", "vibe")
    me = _payload(await mcp.call_tool("whoami", {}))

    assert me["id"] == "chosen-id"
    assert me["harness"] == "vibe"
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


async def test_list_participants_marks_the_caller(daemon):
    mine = build("me", "vibe")
    theirs = build("them", "claude")
    await mine.call_tool("whoami", {})
    await theirs.call_tool("whoami", {})

    rows = _payload(await mine.call_tool("list_participants", {}))
    flags = {r["id"]: r["is_self"] for r in rows}
    assert flags == {"me": True, "them": False}


async def test_a_failing_tool_reports_the_daemon_error(daemon, monkeypatch):
    mcp = build("me", "vibe")
    with pytest.raises(Exception) as exc:
        await mcp.call_tool(
            "spawn_session",
            {"harness": "cursor", "prompt": "hi", "approval": "manual"},
        )
    assert "bad_request" in str(exc.value)
