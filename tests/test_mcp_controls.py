"""The runtime-control tool bodies: thin forwarders with the caller named.

Every control tool reaches the daemon on its frozen RPC name
(``participant.settings.update``, ``participant.controls``), forwards the
calling participant so the daemon can authorize, and returns the daemon's reply
verbatim; the tests assert the exact params that cross the socket.
"""

from __future__ import annotations

import pytest

from tests.test_mcp_tools import FakeClient, resolved, session
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


async def test_get_session_controls_forwards_the_frozen_rpc_with_the_caller_named():
    controls = {
        "capabilities": {
            "send": {"available": True, "transport": "native_runtime"},
            "settings_update": {
                "available": True,
                "transport": "native_runtime",
                "runtime_host": "frontend",
                "supported_fields": ["reasoning_effort"],
            },
        },
        "health": "healthy",
        "settings": {"model": "opus-5"},
        "active_turn": "h#2",
        "queued_handles": ["p-child#4"],
    }
    s = resolved(**{"participant.controls": controls})

    record = await tools.get_session_controls(s, target="p-child")

    assert record == controls
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
