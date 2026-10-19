"""Delegation tool bodies: spawn, send, await, interrupt, scratchpad and session controls."""

from __future__ import annotations

import pytest

from tests.support.mcp_tools import RECORD, FakeClient, resolved, session
from theater.daemon.rpc.jobs import _job_to_dict
from theater.harness.contracts.events import clip
from theater.mcp import tools
from theater.models import Job
from theater.protocol import RemoteError


async def test_spawn_names_the_caller_as_the_parent():
    """Lineage is set here or never — the child cannot know who asked for it."""
    s = resolved()
    child = await tools.spawn_session(s, harness="vibe", prompt="hi", approval="manual")
    assert s.client.params("spawn")["parent_id"] == "p-me"
    assert child["session_id"] == "ses-me"


async def test_spawn_echoes_the_requested_model_and_effort():
    s = resolved(spawn={**RECORD, "model": "anthropic/claude-opus", "reasoning_effort": None})
    child = await tools.spawn_session(s, harness="opencode", approval="manual")
    assert child["model"] == "anthropic/claude-opus"
    assert "reasoning_effort" not in child


async def test_spawn_preserves_unconfirmed_operation_and_diagnostics():
    operation = {
        "handle": "child-job",
        "operation_id": "launch-operation",
        "operation_state": "uncertain",
        "operation_timed_out": True,
        "operation_error": {"code": "terminal_create_failed", "message": "tmux creation failed"},
        "operation_warning": "Terminal launch has not been confirmed. Do not spawn a replacement.",
    }
    child = await tools.spawn_session(
        resolved(spawn={**RECORD, **operation, "addressable": False}),
        harness="vibe",
        prompt="handoff",
        approval="manual",
    )

    assert operation.items() <= child.items()
    assert child["addressable"] is False


async def test_spawn_forwards_an_explicit_provider_override():
    s = resolved()

    await tools.spawn_session(
        s,
        harness="vibe",
        prompt="hi",
        approval="manual",
        provider="provider-remote",
    )

    assert s.client.params("spawn")["provider"] == "provider-remote"


async def test_spawn_accepts_no_prompt():
    """No prompt means the daemon starts a plain CLI and resolves the job."""
    s = resolved()
    await tools.spawn_session(s, harness="vibe", approval="manual")
    assert s.client.params("spawn")["prompt"] is None


async def test_spawn_forwards_response_format_unchanged():
    """MCP forwards the schema hint to the daemon; it does not serialize it."""
    response_format = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    s = resolved()
    await tools.spawn_session(
        s,
        harness="vibe",
        prompt="answer in JSON",
        response_format=response_format,
        approval="manual",
    )
    assert s.client.params("spawn")["response_format"] is response_format


async def test_spawn_forwards_empty_response_format():
    """An empty schema object is still an explicit response_format."""
    response_format = {}
    s = resolved()
    await tools.spawn_session(
        s,
        harness="vibe",
        prompt="answer in JSON",
        response_format=response_format,
        approval="manual",
    )
    assert s.client.params("spawn")["response_format"] is response_format


async def test_spawn_forwards_participant_metadata():
    s = resolved()
    await tools.spawn_session(
        s,
        harness="vibe",
        prompt="work",
        approval="manual",
        name="Metadata-Child",
        description="Implement participant metadata",
    )
    params = s.client.params("spawn")
    assert params["name"] == "Metadata-Child"
    assert params["description"] == "Implement participant metadata"


async def test_spawn_defaults_the_cwd_to_this_process():
    s = resolved()
    await tools.spawn_session(s, harness="vibe", prompt="hi", approval="manual")
    assert s.client.params("spawn")["cwd"]


async def test_spawn_defaults_wiring_to_auto():
    """The frozen default: wiring is always sent, so the daemon sees the choice."""
    s = resolved()
    await tools.spawn_session(s, harness="vibe", prompt="hi", approval="manual")
    assert s.client.params("spawn")["wiring"] == "auto"


async def test_spawn_forwards_an_explicit_wiring_choice():
    s = resolved()
    await tools.spawn_session(
        s,
        harness="vibe",
        prompt="hi",
        approval="manual",
        wiring="legacy",
    )
    assert s.client.params("spawn")["wiring"] == "legacy"


async def test_interrupt_session_identifies_and_names_the_caller():
    s = session(**{"participant.interrupt": {"id": "p-child", "interrupted": True}})

    result = await tools.interrupt_session(s, target="p-child")

    assert result == {"id": "p-child", "interrupted": True}
    assert s.client.params("participant.interrupt") == {
        "target": "p-child",
        "caller_id": "p-me",
    }


async def test_await_names_the_caller_so_a_deadlock_can_be_refused():
    """Waiting on someone who is waiting on you has to be visible to the daemon."""
    s = resolved(**{"jobs.await": []})
    await tools.await_sessions(s, handles=["h#1"], max_wait=5.0)
    p = s.client.params("jobs.await")
    assert (p["caller_id"], p["handles"], p["max_wait"]) == ("p-me", ["h#1"], 5.0)


async def test_await_clamps_max_wait_to_the_clients_ceiling():
    s = resolved(**{"jobs.await": []})
    await tools.await_sessions(s, handles=["h#1"], max_wait=290.0, ceiling=110.0)
    assert s.client.params("jobs.await")["max_wait"] == 110.0


async def test_await_keeps_only_what_the_caller_acts_on():
    """Keep the authoritative participant id alongside its exact job handle."""
    job = {
        "handle": "h#1",
        "caller_id": "p-me",
        "target_id": "p-you",
        "kind": "send",
        "prompt": "do the thing",
        "response_format": {"type": "object"},
        "state": "done",
        "result": "a 2000-char clip nobody asked for",
        "structured_result": '{"answer": 42}',
        "structured_status": "parsed",
        "error_code": None,
        "actor_client_id": None,
        "created_at": 1.0,
        "finished_at": 2.0,
    }
    s = resolved(**{"jobs.await": [job]})
    jobs = await tools.await_sessions(s, handles=["h#1"], max_wait=5.0)
    assert jobs == [
        {
            "handle": "h#1",
            "target_id": "p-you",
            "state": "done",
            "structured_result": {"answer": 42},
            "structured_status": "parsed",
            "finished_at": 2.0,
        }
    ]


async def test_await_preserves_falsy_structured_values():
    rows = [
        {
            "handle": f"h#{index}",
            "state": "done",
            "structured_status": "parsed",
            "structured_result": raw,
        }
        for index, raw in enumerate(("null", "false", "0", "[]", "{}"))
    ]
    s = resolved(**{"jobs.await": rows})
    entries = await tools.await_sessions(s, handles=[row["handle"] for row in rows], max_wait=5.0)
    assert [entry["structured_result"] for entry in entries] == [None, False, 0, [], {}]


async def test_await_inlines_only_a_done_jobs_unparsed_final_answer():
    """The final message saves a read_transcript call; a parsed answer already carries it."""
    long = "x" * 2500
    rows = [
        {"handle": "h#1", "state": "done", "result": long, "structured_status": "unavailable"},
        {"handle": "h#2", "state": "done", "result": ""},
        {"handle": "h#3", "state": "running", "result": "partial"},
        {"handle": "h#4", "state": "crashed", "result": "boom"},
        {"handle": "h#5", "state": "crashed", "error_code": "turn_failed", "result": "why"},
    ]
    s = resolved(**{"jobs.await": rows})
    handles = [row["handle"] for row in rows]
    entries = await tools.await_sessions(s, handles=handles, max_wait=5.0)
    assert entries[0]["result"] == "x" * 2000 + "… (+500 chars)"
    assert [e.get("result") for e in entries[1:]] == [None, None, None, "why"]


async def test_await_does_not_reclip_a_parser_clipped_answer():
    """Re-clipping the parser's 2015-char output rewrote its "+3000" count as "+15"."""
    clipped = clip("x" * 5000)
    spoofed = "y" * 2500 + "… (+1 chars)"  # an agent-written suffix is not clipping provenance
    rows = [
        {"handle": "h#1", "state": "done", "result": clipped},
        {"handle": "h#2", "state": "done", "result": spoofed},
    ]
    s = resolved(**{"jobs.await": rows})
    entries = await tools.await_sessions(s, handles=["h#1", "h#2"], max_wait=5.0)
    assert entries[0]["result"] == clipped
    assert entries[0]["result"].endswith("… (+3000 chars)")
    assert entries[1]["result"] == "y" * 2000 + "… (+512 chars)"


async def test_await_shows_presence_only_while_it_is_not_a_plain_absence():
    presence = {"reason": "terminal focus", "revision": 4, "observed_at": 2.5}
    job = {
        "handle": "h#1",
        "state": "running",
        "human_presence": {"state": "present", "protected": True, **presence},
        "participant_status": "working",
        "await_reason": "timeout",
    }
    absent = {
        "handle": "p-idle",
        "target_id": "p-idle",
        "human_presence": {"state": "absent", "protected": False, **presence},
        "participant_status": "idle",
        "await_reason": "already_absent",
    }
    s = resolved(**{"jobs.await": [job, absent]})
    entries = await tools.await_sessions(s, handles=["h#1", "p-idle"], max_wait=5.0)
    assert entries[0]["human_presence"] == {
        "state": "present",
        "protected": True,
        "reason": "terminal focus",
    }
    assert entries[1] == {
        "handle": "p-idle",
        "target_id": "p-idle",
        "participant_status": "idle",
        "await_reason": "already_absent",
    }


async def test_send_names_the_caller_so_the_reply_comes_back():
    s = resolved(send={"handle": "opaque-job", "target_id": "p-you"})
    result = await tools.send_prompt(s, target_id="p-you", prompt="hello")
    assert result == {"handle": "opaque-job", "target_id": "p-you"}
    p = s.client.params("send")
    assert (p["target"], p["prompt"], p["caller_id"]) == ("p-you", "hello", "p-me")


async def test_send_forwards_response_format_unchanged():
    response_format = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
    }
    s = resolved(send={"handle": "p-you#1"})
    await tools.send_prompt(
        s,
        target_id="p-you",
        prompt="hello",
        response_format=response_format,
    )
    assert s.client.params("send")["response_format"] is response_format


async def test_send_forwards_empty_response_format():
    response_format = {}
    s = resolved(send={"handle": "p-you#1"})
    await tools.send_prompt(
        s,
        target_id="p-you",
        prompt="hello",
        response_format=response_format,
    )
    assert s.client.params("send")["response_format"] is response_format


async def test_scratchpad_write_identifies_and_forwards_exact_rpc_arguments():
    s = session(**{"scratchpad.write": {"namespace": "plan", "key": "abc123"}})
    got = await tools.scratchpad_write(s, value="p-you", namespace="plan")
    assert got == {"namespace": "plan", "key": "abc123"}
    assert s.client.methods == ["hello", "scratchpad.write"]
    assert s.client.params("scratchpad.write") == {
        "namespace": "plan",
        "value": "p-you",
        "key": None,
        "caller_id": "p-me",
    }


@pytest.mark.parametrize(
    ("keys", "keys_only", "expected"),
    [
        (None, None, True),
        (["k1"], None, False),
        ([], None, False),
        (None, False, False),
        (["k1"], True, True),
    ],
)
async def test_scratchpad_get_resolves_defaults_and_forwards(keys, keys_only, expected):
    s = session(**{"scratchpad.get": {"entries": {}, "truncated": False}})
    got = await tools.scratchpad_get(s, namespace="plan", keys=keys, keys_only=keys_only)
    assert got == {"entries": {}, "truncated": False}
    assert s.client.methods == ["hello", "scratchpad.get"]
    assert s.client.params("scratchpad.get") == {
        "namespace": "plan",
        "keys": keys,
        "after_key": None,
        "keys_only": expected,
        "caller_id": "p-me",
    }


async def test_scratchpad_get_drops_what_the_daemon_echoes():
    reply = {"namespace": "plan", "entries": {"k": "v"}, "keys": ["k"], "truncated": False}
    s = session(**{"scratchpad.get": {**reply, "after_key": None}})
    got = await tools.scratchpad_get(s, namespace="plan", keys=["k"])
    assert got == {"entries": {"k": "v"}, "truncated": False}

    s = session(**{"scratchpad.get": {**reply, "entries": [{"key": "k"}], "after_key": "k"}})
    got = await tools.scratchpad_get(s, namespace="plan")
    assert got == {**reply, "entries": [{"key": "k"}], "after_key": "k"}


async def test_scratchpad_delete_identifies_and_forwards_exact_rpc_arguments():
    s = session(**{"scratchpad.delete": {"namespace": "plan", "deleted": ["abc123"]}})
    got = await tools.scratchpad_delete(
        s, namespace="plan", keys=["abc123"], digests=["f" * 64], clear=False
    )
    assert got == {"namespace": "plan", "deleted": ["abc123"]}
    assert s.client.methods == ["hello", "scratchpad.delete"]
    assert s.client.params("scratchpad.delete") == {
        "namespace": "plan",
        "keys": ["abc123"],
        "digests": ["f" * 64],
        "clear": False,
        "caller_id": "p-me",
    }


async def test_update_session_settings_forwards_the_frozen_rpc_with_the_caller_named():
    s = resolved(
        **{
            "participant.settings.update": {
                "applied": True,
                "model": "opus-5",
                "reasoning_effort": None,
            }
        }
    )

    record = await tools.update_session_settings(s, target="p-child", model="opus-5")

    assert record == {"applied": True, "model": "opus-5", "reasoning_effort": None}
    assert s.client.methods == ["participant.settings.update"]
    assert s.client.params("participant.settings.update") == {
        "target": "p-child",
        "model": "opus-5",
        "reasoning_effort": None,
        "caller_id": "p-me",
    }


async def test_update_session_settings_forwards_both_fields():
    """Both fields at once is the daemon's to validate, not a decision made here."""
    s = resolved(**{"participant.settings.update": {"applied": True}})
    await tools.update_session_settings(
        s,
        target="p-child",
        model="opus-5",
        reasoning_effort="medium",
    )
    assert s.client.params("participant.settings.update") == {
        "target": "p-child",
        "model": "opus-5",
        "reasoning_effort": "medium",
        "caller_id": "p-me",
    }


@pytest.mark.parametrize("native_session", ["session", "different", None])
async def test_get_session_controls_forwards_the_frozen_rpc_with_the_caller_named(native_session):
    controls = {
        "capabilities": {
            "send": {"available": True, "transport": "native_runtime", "runtime_host": "frontend"},
            "steer": {
                "available": False,
                "transport": "native_runtime",
                "runtime_host": "frontend",
                "reason": "unsupported",
                "detail": "",
            },
            "settings_update": {
                "available": True,
                "transport": "native_runtime",
                "runtime_host": "frontend",
                "supported_fields": ["reasoning_effort"],
            },
            "interrupt": {
                "available": True,
                "transport": "legacy_keys",
                "runtime_host": "detached_backend",
            },
        },
        "backend_generation": 4,
        "native_session_id": native_session,
        "transcript_identity": {"session_id": "session", "detail": None},
        "health": {"connection": "connected", "diagnostics": []},
        "settings": {"model": "opus-5", "reasoning_effort": None},
        "active_turn": {"native_turn_id": "h#2", "job_handle": None},
        "queued": ["p-child#4"],
        "human_presence": {
            "state": "unknown",
            "protected": True,
            "revision": 0,
            "observed_at": 123.0,
        },
        "empty": {},
        "zero": 0,
    }
    s = resolved(**{"participant.controls": controls})

    record = await tools.get_session_controls(s, target="p-child")

    expected = {
        "capability_route": {"transport": "native_runtime", "runtime_host": "frontend"},
        "capabilities": {
            "send": {"available": True},
            "steer": {"available": False, "reason": "unsupported"},
            "settings_update": {"available": True, "supported_fields": ["reasoning_effort"]},
            "interrupt": {
                "available": True,
                "transport": "legacy_keys",
                "runtime_host": "detached_backend",
            },
        },
        "transcript_identity": {"session_id": "session"},
        "health": {"connection": "connected"},
        "settings": {"model": "opus-5"},
        "active_turn": {"native_turn_id": "h#2"},
        "queued": ["p-child#4"],
        "human_presence": {"state": "unknown", "protected": True},
        "zero": 0,
    }
    if native_session == "different":
        expected["native_session_id"] = "different"
    assert record == expected
    assert controls["capabilities"]["send"]["transport"] == "native_runtime"
    assert s.client.methods == ["participant.controls"]
    assert s.client.params("participant.controls") == {
        "target": "p-child",
        "caller_id": "p-me",
    }


async def test_control_tools_return_daemon_reasons_verbatim():
    """A refusal with a reason is an answer, not an error to reinterpret here."""
    reasons = {
        "capabilities": {"available": [], "unavailable_reasons": {"steer": "wiring_mode"}},
        "reason": "its harness has no runtime; the prompt can only be sent with send",
    }
    s = resolved(**{"participant.controls": reasons})
    record = await tools.get_session_controls(s, target="p-child")
    assert record["capabilities"]["unavailable_reasons"]["steer"] == "wiring_mode"
    assert record["reason"].startswith("its harness has no runtime")


async def test_control_tools_propagate_daemon_errors():
    """The daemon's refusal crosses unchanged; no policy is applied locally."""

    class RefusingClient(FakeClient):
        async def call(self, method, **params):
            self.calls.append((method, params))
            raise RemoteError("busy", f"participant 'p-child' is working (from {method})")

    s = tools.Session(participant_id="p-me", harness="vibe", client=RefusingClient())
    s._resolved = True

    for call, kwargs in (
        (tools.update_session_settings, {"target": "p-child", "model": "opus-5"}),
        (tools.get_session_controls, {"target": "p-child"}),
    ):
        with pytest.raises(RemoteError) as exc:
            await call(s, **kwargs)
        assert exc.value.code == "busy"
        assert "is working" in exc.value.message


@pytest.mark.parametrize(
    ("state", "code", "detail", "explanation", "keeps_result"),
    [
        (
            "crashed",
            "delivery_unknown",
            "WARNING: native work may still be running. Inspect the participant before re-sending.",
            "WARNING: native work may still be running. Inspect the participant before re-sending.",
            False,
        ),
        ("crashed", "daemon_restarted", "daemon restarted", "daemon restarted", False),
        ("killed", "unknown_code", "termination detail", "termination detail", False),
        ("crashed", "tmux_restarted", "old detail", "spawn_session(resume=<target_id>)", False),
        ("crashed", "prompt_never_seen", "old detail", "read_transcript before re-sending", False),
        ("crashed", "turn_failed", "backend failed", "see result for the detail", True),
        ("killed", "cancelled", "cancelled before its source ended", "nothing reached", True),
    ],
)
async def test_await_preserves_failure_guidance(state, code, detail, explanation, keeps_result):
    job = Job(
        handle="child#1",
        caller_id="p-me",
        target_id="child",
        kind="send",
        prompt="task",
        state=state,
        result=detail,
        error_code=code,
        created_at=1.0,
        finished_at=2.0,
    )
    session = resolved(**{"jobs.await": [_job_to_dict(job)]})

    [entry] = await tools.await_sessions(session, handles=[job.handle], max_wait=0)

    assert explanation in entry["error"]
    assert entry["target_id"] == job.target_id
    assert entry.get("result") == (detail if keeps_result else None)
