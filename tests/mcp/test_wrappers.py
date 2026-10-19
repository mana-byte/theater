"""The registered MCP wrappers forward their arguments to the tool bodies unchanged."""

from __future__ import annotations

import json

import pytest

from tests.support.mcp_tools import tool_payload
from theater.mcp import tools as mcp_tools
from theater.mcp.server import build


@pytest.mark.parametrize("preview", [False, True])
async def test_skill_tool_wrappers_forward_to_tool_bodies(monkeypatch, preview):
    calls = []

    async def fake_list_skills(session, *, play=None) -> dict:
        calls.append(("list", session, play))
        return {"skills": [], "rejections": []}

    async def fake_load_skill(session, *, name: str, preview: bool) -> dict:
        calls.append(("load", session, name, preview))
        return {"name": name, "content": "# Alpha\n"}

    monkeypatch.setattr(mcp_tools, "list_skills", fake_list_skills)
    monkeypatch.setattr(mcp_tools, "load_skill", fake_load_skill)

    mcp = build("p1", "vibe")
    assert tool_payload(await mcp.call_tool("list_skills", {})) == {"skills": [], "rejections": []}
    args = {"name": "alpha", **({"preview": True} if preview else {})}
    assert tool_payload(await mcp.call_tool("load_skill", args)) == {
        "name": "alpha",
        "content": "# Alpha\n",
    }
    assert [call[0] for call in calls] == ["list", "load"]
    assert calls[1][2:] == ("alpha", preview)


async def test_control_tool_wrappers_forward_to_tool_bodies(monkeypatch):
    calls = {}

    async def fake_send(session, **kwargs):
        calls["send"] = (session, kwargs)
        return {"handle": "p-child#4", "route": "queued"}

    async def fake_settings(session, **kwargs):
        calls["settings"] = (session, kwargs)
        return {"applied": True}

    async def fake_controls(session, **kwargs):
        calls["controls"] = (session, kwargs)
        return {"capabilities": {}}

    monkeypatch.setattr(mcp_tools, "send_prompt", fake_send)
    monkeypatch.setattr(mcp_tools, "update_session_settings", fake_settings)
    monkeypatch.setattr(mcp_tools, "get_session_controls", fake_controls)

    mcp = build("p1", "vibe")
    response_format = {"type": "object"}
    assert tool_payload(
        await mcp.call_tool(
            "send",
            {
                "target": "child",
                "prompt": "later",
                "if_busy": "queue",
                "job_handle": "h#2",
                "response_format": response_format,
                "after": "Writer",
                "skills": ["theater-debate"],
            },
        )
    ) == {"handle": "p-child#4", "route": "queued"}
    assert tool_payload(
        await mcp.call_tool("update_session_settings", {"target": "child", "model": "opus-5"})
    ) == {"applied": True}
    assert tool_payload(await mcp.call_tool("get_session_controls", {"target": "child"})) == {
        "capabilities": {}
    }

    assert all(isinstance(call[0], mcp_tools.Session) for call in calls.values())
    assert calls["send"][1] == {
        "target_id": "child",
        "prompt": "later",
        "if_busy": "queue",
        "job_handle": "h#2",
        "response_format": response_format,
        "after": "Writer",
        "skills": ["theater-debate"],
    }
    assert calls["settings"][1] == {
        "target": "child",
        "model": "opus-5",
        "reasoning_effort": None,
    }
    assert calls["controls"][1] == {"target": "child"}


async def test_spawn_wrapper_forwards_wiring_unchanged(monkeypatch):
    calls = {}

    async def fake_spawn(session, **kwargs):
        calls["spawn"] = (session, kwargs)
        return {"ok": "spawn"}

    monkeypatch.setattr(mcp_tools, "spawn_session", fake_spawn)

    mcp = build("p1", "vibe")
    assert tool_payload(
        await mcp.call_tool(
            "spawn_session",
            {"harness": "vibe", "approval": "manual", "wiring": "legacy"},
        )
    ) == {"ok": "spawn"}
    assert calls["spawn"][1]["wiring"] == "legacy"


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

    assert tool_payload(
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
    assert tool_payload(
        await mcp.call_tool(
            "send",
            {
                "target": "p-child",
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
    assert send_kwargs["target_id"] == "p-child"


async def test_participant_metadata_wrappers_forward_to_tool_bodies(monkeypatch):
    calls = {}

    async def fake_spawn(session, **kwargs):
        calls["spawn"] = (session, kwargs)
        return {"ok": "spawn"}

    async def fake_update(session, **kwargs):
        calls["update"] = (session, kwargs)
        return {"ok": "update"}

    monkeypatch.setattr(mcp_tools, "spawn_session", fake_spawn)
    monkeypatch.setattr(mcp_tools, "update_participant", fake_update)

    mcp = build("p1", "vibe")
    assert tool_payload(
        await mcp.call_tool(
            "spawn_session",
            {
                "harness": "vibe",
                "approval": "edits",
                "name": "Metadata-Child",
                "description": "Implement metadata",
            },
        )
    ) == {"ok": "spawn"}
    assert tool_payload(
        await mcp.call_tool(
            "update_participant",
            {"target": "child", "description": "Updated metadata"},
        )
    ) == {"ok": "update"}
    assert calls["spawn"][1]["name"] == "Metadata-Child"
    assert calls["spawn"][1]["description"] == "Implement metadata"
    assert calls["update"][1] == {
        "target": "child",
        "name": None,
        "description": "Updated metadata",
    }


async def test_interrupt_wrapper_forwards_to_tool_body(monkeypatch):
    calls = {}

    async def fake_interrupt(session, **kwargs):
        calls["interrupt"] = (session, kwargs)
        return {"interrupted": True}

    monkeypatch.setattr(mcp_tools, "interrupt_session", fake_interrupt)

    mcp = build("p1", "vibe")
    assert tool_payload(await mcp.call_tool("interrupt_session", {"target": "child"})) == {
        "interrupted": True
    }
    interrupt_session, kwargs = calls["interrupt"]
    assert isinstance(interrupt_session, mcp_tools.Session)
    assert kwargs == {"target": "child"}


async def test_new_tool_wrappers_forward_to_tool_bodies(monkeypatch):
    calls = []

    async def fake_scratchpad_write(
        session, *, value: str, namespace: str, key: str | None = None
    ) -> dict:
        calls.append(("scratchpad_write", session, namespace, value, key))
        return {"namespace": namespace, "key": "abc123"}

    async def fake_scratchpad_get(
        session,
        *,
        namespace: str,
        keys: list[str] | None = None,
        after_key: str | None = None,
        keys_only: bool | None = None,
    ) -> dict:
        calls.append(("scratchpad_get", session, namespace, keys, after_key, keys_only))
        return {"namespace": namespace, "entries": [], "keys": [], "truncated": False}

    async def fake_scratchpad_delete(
        session,
        *,
        namespace: str,
        keys: list[str] | None = None,
        digests: list[str] | None = None,
        clear: bool = False,
    ) -> dict:
        calls.append(("scratchpad_delete", session, namespace, keys, digests, clear))
        return {"namespace": namespace, "deleted": keys}

    monkeypatch.setattr(mcp_tools, "scratchpad_write", fake_scratchpad_write)
    monkeypatch.setattr(mcp_tools, "scratchpad_get", fake_scratchpad_get)
    monkeypatch.setattr(mcp_tools, "scratchpad_delete", fake_scratchpad_delete)

    mcp = build("p1", "vibe")
    assert tool_payload(
        await mcp.call_tool(
            "scratchpad_write",
            {"namespace": "plan", "value": "p-you"},
        )
    ) == {"namespace": "plan", "key": "abc123"}
    assert tool_payload(await mcp.call_tool("scratchpad_get", {"namespace": "plan"})) == {
        "namespace": "plan",
        "entries": [],
        "keys": [],
        "truncated": False,
    }
    assert tool_payload(
        await mcp.call_tool("scratchpad_delete", {"namespace": "plan", "keys": ["abc123"]})
    ) == {"namespace": "plan", "deleted": ["abc123"]}

    assert [(call[0], *call[2:]) for call in calls] == [
        ("scratchpad_write", "plan", "p-you", None),
        ("scratchpad_get", "plan", None, None, None),
        ("scratchpad_delete", "plan", ["abc123"], None, False),
    ]
    assert all(isinstance(call[1], mcp_tools.Session) for call in calls)


async def test_handoff_arguments_reach_the_tools_and_are_taught_once(mcp_daemon):
    mcp = build("p1", "vibe")
    tools = {t.name: t for t in await mcp.list_tools()}
    descriptions = {name: " ".join(t.description.split()) for name, t in tools.items()}

    spawn, send = tools["spawn_session"].input_schema, tools["send"].input_schema
    assert {"after", "task_kind"} <= set(spawn["properties"])
    assert "after" in send["properties"] and "task_kind" not in send["properties"]
    assert "independent_review" in json.dumps(spawn["properties"]["task_kind"])
    assert "source_not_reviewable" in descriptions["spawn_session"]
    assert "pinned commit" in descriptions["spawn_session"]
    assert "queued" in descriptions["send"] and "cancel_job" in descriptions["send"]
    assert "or a send to yourself" in descriptions["send"]  # human_present is not refuse-only
    assert "waiting for its `after` source" in descriptions["cancel_job"]
    # Shared guidance teaches natural send and `after`, and no longer sends agents to the
    # transcript after every await.
    assert 'after="Writer"' in mcp.instructions
    assert "read_transcript to read what it actually said" not in mcp.instructions
    assert "diagnose a stuck, failed or surprising child" in mcp.instructions
    assert "Diagnose" in descriptions["read_transcript"]
    assert "Diagnose" in descriptions["get_session_controls"]


async def test_after_and_task_kind_are_forwarded_unchanged_to_the_daemon():
    from theater.mcp.session import Session

    class Recorder:
        def __init__(self):
            self.calls = []

        async def call(self, method, **params):
            self.calls.append((method, params))
            keys = ("name", "description", "tier", "status", "cwd", "branch", "session_id")
            return {
                "id": "c1",
                "handle": "c1",
                "harness": "codex",
                "parent_id": "p1",
                "addressable": False,
                "handoff": {"state": "waiting"},
                **dict.fromkeys(keys),
            }

    client = Recorder()
    session = Session(participant_id="p1", harness="vibe", client=client, _resolved=True)

    spawned = await mcp_tools.spawn_session(
        session,
        harness="codex",
        approval="yolo",
        prompt="Review",
        worktree=True,
        after="Writer",
        task_kind="independent_review",
        skills=["theater-debate"],
    )
    await mcp_tools.send_prompt(session, target_id="Bob", prompt="x", after="Writer")

    (_, spawn_params), (_, send_params) = client.calls
    assert (spawn_params["after"], spawn_params["task_kind"]) == ("Writer", "independent_review")
    assert send_params["after"] == "Writer"
    assert "task_kind" not in send_params
    assert (spawn_params["skills"], send_params["skills"]) == (["theater-debate"], None)
    assert spawned["handoff"] == {"state": "waiting"}
