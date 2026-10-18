"""Control tools preserve daemon decisions while trimming repetitive diagnostics."""

from __future__ import annotations

import pytest

from tests.support.mcp_tools import FakeClient, resolved, session
from theater.mcp import tools
from theater.protocol import RemoteError


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


async def test_update_session_settings_identifies_first():
    s = session(**{"participant.settings.update": {"applied": True}})
    await tools.update_session_settings(s, target="p-child", model="opus-5")
    assert s.client.methods == ["hello", "participant.settings.update"]


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


async def test_get_session_controls_identifies_first():
    s = session(**{"participant.controls": {}})
    await tools.get_session_controls(s, target="p-child")
    assert s.client.methods == ["hello", "participant.controls"]


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
