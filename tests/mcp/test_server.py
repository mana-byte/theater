"""The MCP surface driven through MCPServer against a real daemon."""

from __future__ import annotations

import json

import pytest

from tests.support.mcp_tools import tool_payload
from tests.support.presence import FakePresence
from theater import paths
from theater.client import DaemonClient
from theater.constants.response_contract import CONTRACT_PRESET_NAMES
from theater.daemon.response_contracts.contract import StrictContract
from theater.daemon.response_contracts.presets import PRESET_SCHEMAS
from theater.daemon.server import Daemon
from theater.harness.contracts.events import clip
from theater.mcp.server import await_ceiling, build
from theater.models import JobState


@pytest.fixture
def presence(mcp_daemon):
    """No human at any pane: presence holds are exercised in their own file."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(mcp_daemon, "presence", FakePresence(), raising=False)
        yield


async def test_tools_are_registered(mcp_daemon):
    tools = await build("p1", "vibe").list_tools()
    assert {t.name for t in tools} == {
        "whoami",
        "list_participants",
        "list_harnesses",
        "list_skills",
        "load_skill",
        "spawn_session",
        "update_participant",
        "await_sessions",
        "send",
        "update_session_settings",
        "get_session_controls",
        "interrupt_session",
        "cancel_job",
        "scratchpad_write",
        "scratchpad_get",
        "scratchpad_delete",
        "read_transcript",
        "terminate_session",
        "adopt_session",
        "recall",
        "recall_read",
    }


@pytest.mark.parametrize("harness", ["codex", "vibe"])
async def test_control_and_wait_toolsets_partition_the_mcp_surface(mcp_daemon, harness):
    control = {tool.name for tool in await build("p1", harness, "control").list_tools()}
    wait = {tool.name for tool in await build("p1", harness, "wait").list_tools()}

    assert "await_sessions" not in control
    assert wait == {"await_sessions"}
    assert control | wait == {tool.name for tool in await build("p1", harness).list_tools()}


async def test_wait_toolset_reuses_the_control_participant_identity(mcp_daemon, presence):
    control = build("p1", "vibe", "control")
    assert tool_payload(await control.call_tool("whoami", {}))["id"] == "p1"
    mcp_daemon.jobs.create(handle="done", caller_id="p1", target_id=None, kind="test")
    mcp_daemon.jobs.finish("done", state=JobState.DONE)

    wait = build("p1", "vibe", "wait")
    result = tool_payload(await wait.call_tool("await_sessions", {"handles": ["done"]}))
    assert len(result) == 1
    assert result[0]["handle"] == "done"
    assert result[0]["state"] == "done"

    async with DaemonClient(autostart=False) as client:
        rows = await client.call("participants.list")
    assert [row["id"] for row in rows] == ["p1"]


async def test_await_returns_a_native_structured_result(mcp_daemon, presence):
    mcp_daemon.jobs.create(
        handle="structured",
        caller_id="p1",
        target_id=None,
        kind="send",
        response_format="json",
    )
    mcp_daemon.jobs.finish(
        "structured",
        state=JobState.DONE,
        result='{"answer":42}',
        raw_result='{"answer":42}',
    )

    result = await build("p1", "vibe", "wait").call_tool(
        "await_sessions", {"handles": ["structured"]}
    )
    entry = tool_payload(result)[0]
    assert entry["structured_status"] == "parsed"
    assert entry["structured_result"] == {"answer": 42}


async def test_await_exposes_safe_failure_details_and_the_authoritative_target(
    mcp_daemon, presence
):
    schema = {"type": "object", "properties": {"summary": {"type": "string", "maxLength": 1500}}}
    answer = json.dumps({"summary": "SECRET" + "x" * 2500})
    job = mcp_daemon.jobs.create(
        handle="opaque-job",
        caller_id="p1",
        target_id="the-worker",
        kind="send",
        response_format=StrictContract("custom", json.dumps(schema)).envelope(),
    )
    mcp_daemon.jobs.finish(job.handle, state=JobState.DONE, result=clip(answer), raw_result=answer)

    result = await build("p1", "vibe", "wait").call_tool(
        "await_sessions", {"handles": [job.handle], "max_wait": 0}
    )
    entry = tool_payload(result)[0]

    assert entry["target_id"] == "the-worker"
    assert entry["structured_status"] == "unable_to_parse"
    assert entry["structured_error"] == {
        "event": "unsupported_route",
        "reason": "schema",
        "issues": [{"path": "/summary", "keyword": "maxLength", "limit": 1500, "actual": 2506}],
    }
    assert "SECRET" not in json.dumps(entry["structured_error"])


async def test_bare_dict_tools_return_one_native_object_with_the_text_mirror(mcp_daemon):
    mcp = build("p1", "vibe")
    whoami = next(tool for tool in await mcp.list_tools() if tool.name == "whoami")
    result = await mcp.call_tool("whoami", {"tail": 5})
    bulk = await mcp.call_tool("scratchpad_get", {"namespace": "empty"})

    assert whoami.output_schema is None
    assert result.structured_content == json.loads(result.content[0].text)
    assert result.structured_content["ignored_arguments"] == [
        {"tail": "removed; page with next_cursor"}
    ]
    assert bulk.structured_content is None


def test_mcp_toolset_must_be_known():
    with pytest.raises(ValueError, match="unknown MCP toolset"):
        build("p1", "vibe", "unknown")


async def test_every_tool_is_documented(mcp_daemon):
    """An agent picks tools by reading descriptions, so blank ones are bugs."""
    for tool in await build("p1", "vibe").list_tools():
        assert tool.description and tool.description.strip(), tool.name


async def test_descriptions_pin_non_obvious_wire_contracts(mcp_daemon):
    mcp = build("p1", "vibe")
    descriptions = {
        tool.name: " ".join(tool.description.split()) for tool in await mcp.list_tools()
    }

    assert "final message ends that job" in " ".join(mcp.instructions.split())
    assert "done, crashed, or killed" in descriptions["await_sessions"]
    assert "native JSON value" in descriptions["await_sessions"]
    assert '{"evidence":"none"}' in descriptions["await_sessions"]
    assert "successful terminal binding" in descriptions["spawn_session"]
    assert "does not guarantee launch success" in descriptions["spawn_session"]
    assert "validated JSON" in descriptions["spawn_session"]
    assert "only a human can answer it" in descriptions["spawn_session"]
    assert "human_present" in descriptions["terminate_session"]
    assert "already_dead" in descriptions["terminate_session"]
    assert "nullable" in descriptions["scratchpad_get"]
    assert "else stop and report it" in descriptions["scratchpad_get"]
    assert "may be combined" in descriptions["scratchpad_delete"]
    assert "partial text chunk" in descriptions["read_transcript"]
    assert "middle_cursor" in descriptions["read_transcript"]
    assert "does not prove the edit was never committed" in descriptions["recall_read"]
    assert "unable_to_parse" in descriptions["await_sessions"]
    for preset in CONTRACT_PRESET_NAMES:
        assert f'{{"$theater": "{preset}"}}' in descriptions["spawn_session"], preset
        for field in PRESET_SCHEMAS[preset]["required"]:
            assert field in descriptions["spawn_session"], (preset, field)
    assert '"custom"' in descriptions["spawn_session"]
    assert "same contract as spawn_session" in descriptions["send"]


@pytest.mark.parametrize(
    ("harness", "env", "ceiling", "cap"),
    [
        ("claude", None, 110.0, "cap 110."),
        ("claude", "120000", 110.0, "cap 110."),
        ("claude", "360000", 350.0, "cap 300."),
        ("claude", "0", None, "cap 300."),
        ("vibe", "120000", None, "cap 300."),
    ],
)
async def test_await_cap_stays_under_claudes_background_threshold(
    mcp_daemon, monkeypatch, harness, env, ceiling, cap
):
    """Claude backgrounds a call past its threshold and the await result never arrives."""
    if env is None:
        monkeypatch.delenv("CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS", env)
    assert await_ceiling(harness) == ceiling
    tools = {t.name: t for t in await build("p1", harness, "wait").list_tools()}
    assert cap in tools["await_sessions"].description


async def test_the_server_carries_its_instructions(mcp_daemon):
    assert build("p1", "vibe").instructions.strip()


async def test_spawn_session_forces_a_choice_of_approval(mcp_daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    required = schema["spawn_session"]["required"]
    assert "approval" in required
    assert "prompt" not in required
    assert "cwd" not in required


async def test_spawn_session_worktree_schema_accepts_name_bool_or_null(mcp_daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    worktree = schema["spawn_session"]["properties"]["worktree"]
    assert {entry["type"] for entry in worktree["anyOf"]} == {"string", "boolean", "null"}


async def test_spawn_session_exposes_provider_override_and_bridge_guidance(mcp_daemon):
    tools = await build("p1", "vibe").list_tools()
    spawn = next(tool for tool in tools if tool.name == "spawn_session")

    assert "provider" in spawn.input_schema["properties"]
    assert "provider" not in spawn.input_schema["required"]
    assert "regie bridge start" in spawn.description


async def test_participant_metadata_schemas_are_optional_and_nullable(mcp_daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}

    spawn = schema["spawn_session"]
    assert {"name", "description"} <= set(spawn["properties"])
    assert "name" not in spawn["required"]
    assert "description" not in spawn["required"]

    update = schema["update_participant"]
    assert set(update["properties"]) == {"target", "name", "description"}
    assert update.get("required", []) == []


def _assert_nullable_object_schema(prop: dict) -> None:
    assert prop["default"] is None
    variants = prop["anyOf"]
    assert {"type": "null"} in variants
    objects = [variant for variant in variants if variant.get("type") == "object"]
    assert objects
    assert objects[0]["additionalProperties"] is True


async def test_response_format_parameters_have_nullable_object_schema(mcp_daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}

    for name in ("spawn_session", "send"):
        prop = schema[name]["properties"]["response_format"]
        _assert_nullable_object_schema(prop)
        assert "response_format" not in schema[name].get("required", [])


async def test_new_tool_schemas_match_public_signatures(mcp_daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}

    assert schema["scratchpad_write"]["required"] == ["value", "namespace"]
    assert schema["scratchpad_get"]["required"] == ["namespace"]
    assert schema["scratchpad_delete"]["required"] == ["namespace"]
    assert schema["list_skills"].get("required", []) == []
    assert schema["load_skill"]["required"] == ["name"]


async def test_scratchpad_descriptions_state_machine_wide_ttl_scope(mcp_daemon):
    tools = {tool.name: tool for tool in await build("p1", "vibe").list_tools()}

    for name in ("scratchpad_write", "scratchpad_get"):
        description = tools[name].description
        assert "machine-wide" in description
        assert "TTL" in description
        assert "scopes access to your" not in description
        assert "not available outside a git repository" not in description


async def test_skill_tools_round_trip_through_the_daemon(mcp_daemon):
    mcp = build("p1", "vibe")

    listed = tool_payload(await mcp.call_tool("list_skills", {}))
    assert [skill["name"] for skill in listed["skills"]] == [
        "theater-configure",
        "theater-debate",
        "theater-delegate",
        "theater-recover-tmux",
    ]
    assert all("content" not in skill for skill in listed["skills"])
    hierarchy, pair = listed["plays"]
    assert hierarchy["master"] == "hierarchy/master"
    assert hierarchy["disposable"] == ["hierarchy/member"]
    assert [position["name"] for position in hierarchy["positions"]] == [
        "hierarchy/master",
        "hierarchy/lead",
        "hierarchy/member",
    ]
    assert pair["master"] == "pair/reviewer"
    assert pair["disposable"] == ["pair/builder"]
    assert tool_payload(await mcp.call_tool("list_skills", {"play": "hierarchy"}))["plays"] == [
        hierarchy
    ]

    loaded = tool_payload(await mcp.call_tool("load_skill", {"name": "theater-debate"}))
    assert loaded["name"] == "theater-debate"
    assert loaded["source"] == "builtin"
    assert loaded["content"].startswith("---\nname: theater-debate\n")


async def test_a_disabled_skill_is_hidden_from_list_and_refused_on_load(theater_home):
    paths.config_path().write_text('[skills]\ndisabled = ["theater-configure"]\n')

    d = Daemon()
    await d.start()
    try:
        mcp = build("p1", "vibe")

        listed = tool_payload(await mcp.call_tool("list_skills", {}))
        assert [skill["name"] for skill in listed["skills"]] == [
            "theater-debate",
            "theater-delegate",
            "theater-recover-tmux",
        ]

        refused = await mcp.call_tool("load_skill", {"name": "theater-configure"})
        assert refused.structured_content["error"]["code"] == "not_found"

        loaded = tool_payload(await mcp.call_tool("load_skill", {"name": "theater-debate"}))
        assert loaded["name"] == "theater-debate"
        assert loaded["content"].startswith("---\nname: theater-debate\n")
    finally:
        await d.aclose()


async def test_targeting_tool_schemas_use_target_not_target_id(mcp_daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    transcript = schema["read_transcript"]
    assert "target" in transcript["properties"]
    assert "target_id" not in transcript["properties"]
    assert set(transcript["properties"]) == {"target", "cursor", "roles"}
    assert transcript["properties"]["cursor"]["default"] is None
    assert transcript["required"] == ["target"]

    send = schema["send"]
    assert "target" in send["properties"]
    assert "target_id" not in send["properties"]
    assert set(send["properties"]) == {
        "target",
        "prompt",
        "if_busy",
        "job_handle",
        "response_format",
        "after",
        "skills",
    }
    assert send["properties"]["if_busy"]["default"] == "auto"
    assert set(send["properties"]["if_busy"]["enum"]) == {
        "auto",
        "refuse",
        "queue",
        "amend",
        "replace",
    }
    assert set(send["required"]) == {"target", "prompt"}


async def test_skills_are_optional_name_lists_on_spawn_and_send(mcp_daemon):
    mcp = build("p1", "vibe")
    tools = {t.name: t for t in await mcp.list_tools()}

    for name in ("spawn_session", "send"):
        schema = tools[name].input_schema
        assert schema["properties"]["skills"]["default"] is None
        assert {"type": "array", "items": {"type": "string"}} in schema["properties"]["skills"][
            "anyOf"
        ]
        assert "skills" not in schema.get("required", [])
    assert "a child never inherits your skills" in " ".join(
        tools["spawn_session"].description.split()
    )
    assert "skills=[...]" in mcp.instructions


async def test_kill_schema_uses_target_not_target_id(mcp_daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    kill = schema["terminate_session"]
    assert set(kill["properties"]) == {"target", "forget"}
    assert kill["required"] == ["target"]


async def test_spawn_wiring_schema_offers_auto_native_legacy_daemon_default_auto(mcp_daemon):
    """Wiring is a spawn-surface choice; approval keeps its no-default rule."""
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    spawn = schema["spawn_session"]
    wiring = spawn["properties"]["wiring"]
    assert wiring["default"] == "auto"
    assert set(wiring["enum"]) == {"auto", "native", "legacy"}
    assert "wiring" not in spawn["required"]
    assert "approval" in spawn["required"]


async def test_control_tool_schemas_are_thin_targeted_forwards(mcp_daemon):
    """The tools carry no policy surface: one target, the change, nothing else."""
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}

    settings = schema["update_session_settings"]
    assert set(settings["properties"]) == {"target", "model", "reasoning_effort"}
    assert settings["required"] == ["target"]

    controls = schema["get_session_controls"]
    assert set(controls["properties"]) == {"target"}
    assert controls["required"] == ["target"]


async def test_interrupt_description_states_turn_and_followup_cancellation(mcp_daemon):
    """The frozen semantics: the active turn AND undelivered queued followups."""
    tools = {t.name: t for t in await build("p1", "vibe").list_tools()}
    description = tools["interrupt_session"].description
    assert "queued followup" in description
    assert "undelivered" in description
    assert 'error_code "interrupted"' in description
    assert "cancelled_followups" in description
    assert "busy" in description
    assert "stale_target" in description


async def test_control_tool_descriptions_point_policy_at_the_daemon(mcp_daemon):
    """Unsupported actions show the daemon's reason; nothing is decided locally."""
    tools = {t.name: t for t in await build("p1", "vibe").list_tools()}
    assert "daemon" in tools["update_session_settings"].description
    assert "reason" in tools["get_session_controls"].description


async def test_send_description_states_daemon_selected_delivery(mcp_daemon):
    """Send follows the daemon's selected wiring — it stopped being always-tmux."""
    tools = {t.name: t for t in await build("p1", "vibe").list_tools()}
    description = tools["send"].description
    assert "native runtime" in description
    assert "terminal provider" in description
    assert "send-keys" not in description
    assert "route is chosen before" in description
    assert "never replayed" in description
    assert "verified physical inbound route" in description
    assert "not ownership-gated" in description


async def test_spawn_wiring_description_names_the_daemon_owned_rollout(mcp_daemon):
    """Auto is the daemon's call while the rollout is gated, not a capability claim."""
    spawn = next(t for t in await build("p1", "vibe").list_tools() if t.name == "spawn_session")
    description = spawn.description
    assert "rollout" in description
    assert "legacy" in description
    assert "terminal provider" in description
    assert "per capability" in description


async def test_list_participants_schema_exposes_keyset_pagination(mcp_daemon):
    schema = {t.name: t.input_schema for t in await build("p1", "vibe").list_tools()}
    props = schema["list_participants"]["properties"]
    assert {"limit", "after_id"} <= set(props)


async def test_whoami_registers_on_first_call(mcp_daemon):
    mcp = build("chosen-id", "vibe")
    me = tool_payload(await mcp.call_tool("whoami", {}))

    assert me["id"] == "chosen-id"
    assert me["harness"] == "vibe"
    assert "session_id" not in me  # omitted until the observer finds the transcript
    # No pane reached us: TMUX_PANE is not in the SDK's env allowlist.
    assert me["tier"] == "external"
    assert "git" not in me
    live = tool_payload(await mcp.call_tool("whoami", {"include_git": True}))
    assert set(live["git"]) == {"pin_state", "commit_sha", "branch", "dirty_entries", "reason"}

    async with DaemonClient(autostart=False) as c:
        rows = await c.call("participants.list")
    assert [r["id"] for r in rows] == ["chosen-id"]


async def test_list_participants_marks_the_caller(mcp_daemon):
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

    rows = tool_payload(await mine.call_tool("list_participants", {}))
    flags = {r["id"]: r["is_self"] for r in rows if "is_self" in r}
    assert flags == {"me": True}
    session_ids = {r["id"]: r.get("session_id") for r in rows}
    assert session_ids == {"me": None, "them": "ses-them"}


@pytest.mark.parametrize(
    ("harness", "is_error", "prefix"),
    [("codex", True, "bad_request: "), ("vibe", False, "REFUSED bad_request: ")],
)
async def test_a_refusal_is_one_envelope_in_the_harness_delivery_mode(
    mcp_daemon, harness, is_error, prefix
):
    """Vibe hides isError content, so its manifest asks for the envelope as plain text."""
    mcp = build("me", harness)
    result = await mcp.call_tool(
        "spawn_session", {"harness": "cursor", "prompt": "hi", "approval": "manual"}
    )
    head, line = result.content[0].text.split("\n")
    assert result.is_error is is_error
    assert head.startswith(prefix) and "cursor" in head
    assert json.loads(line) == result.structured_content
    assert result.structured_content["ok"] is False
    assert result.structured_content["error"]["retryable"] is False


async def test_a_daemon_that_does_not_answer_is_daemon_unavailable(mcp_daemon, monkeypatch):
    mcp = build("me", "codex")

    async def broken(*args, **kwargs):
        raise ConnectionError("socket closed")

    monkeypatch.setattr(DaemonClient, "call", broken)
    error = (await mcp.call_tool("whoami", {})).structured_content["error"]
    assert error["code"] == "daemon_unavailable"
    assert error["side_effect"] == "unknown" and error["next"]


async def test_unknown_arguments_are_reported_not_dropped(mcp_daemon):
    mcp = build("me", "vibe")
    payload = tool_payload(await mcp.call_tool("whoami", {"tail": 5, "x": 1}))
    assert payload["id"] == "me"
    assert payload["ignored_arguments"] == [{"tail": "removed; page with next_cursor"}, "x"]
    rows = await mcp.call_tool("list_harnesses", {"harness": "vibe"})
    assert "ignored_arguments" not in rows.model_dump_json()


def test_only_daemon_transport_failures_map_to_daemon_unavailable():
    """A removed cwd (FileNotFoundError) is the caller's problem, not a daemon outage."""
    from theater.mcp.refusals import refusal_envelope

    assert refusal_envelope(FileNotFoundError("cwd")) is None
    envelope = refusal_envelope(ConnectionError("daemon closed the connection"))
    assert envelope is not None and envelope["error"]["code"] == "daemon_unavailable"
