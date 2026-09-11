"""The runtime-control tool bodies: thin forwarders with the caller named.

Wave 4B's contract, pinned here: every control tool reaches the daemon on
exactly its frozen RPC name (``participant.steer``, ``participant.queue_followup``,
``participant.settings.update``, ``participant.controls``), forwards the
actual calling participant so the daemon can authorize, and returns the
daemon's reply — including its capability and unavailability reasons —
verbatim. Nothing here decides policy locally; the tests hold that line by
asserting the exact params that cross the socket.
"""

from __future__ import annotations

import pytest

from tests.test_mcp_tools import FakeClient, resolved, session
from theater.mcp import tools
from theater.protocol import RemoteError


async def test_steer_forwards_the_frozen_rpc_with_the_caller_named():
    """The daemon authorizes against caller_id; the tool must not lose it."""
    s = resolved(**{"participant.steer": {"handle": "h#2", "amended": True}})

    record = await tools.steer_session(s, target="p-child", prompt="amend")

    assert record == {"handle": "h#2", "amended": True}
    assert s.client.methods == ["participant.steer"]
    assert s.client.params("participant.steer") == {
        "target": "p-child",
        "prompt": "amend",
        "job_handle": None,
        "caller_id": "p-me",
    }


async def test_steer_identifies_first_or_the_daemon_cannot_authorize():
    s = session(**{"participant.steer": {"handle": "h#2", "amended": True}})
    await tools.steer_session(s, target="p-child", prompt="amend")
    assert s.client.methods == ["hello", "participant.steer"]


async def test_steer_forwards_the_expected_job_handle():
    """The caller may pin which job it expects; a mismatch is the daemon's to refuse."""
    s = resolved(**{"participant.steer": {"handle": "h#2", "amended": True}})
    await tools.steer_session(s, target="p-child", prompt="amend", job_handle="h#2")
    assert s.client.params("participant.steer")["job_handle"] == "h#2"


async def test_queue_followup_forwards_the_frozen_rpc_with_the_caller_named():
    s = resolved(**{"participant.queue_followup": {"handle": "p-child#4", "state": "running"}})

    record = await tools.queue_followup(s, target="p-child", prompt="later")

    assert record == {"handle": "p-child#4", "state": "running"}
    assert s.client.methods == ["participant.queue_followup"]
    assert s.client.params("participant.queue_followup") == {
        "target": "p-child",
        "prompt": "later",
        "response_format": None,
        "caller_id": "p-me",
    }


async def test_queue_followup_forwards_response_format_unchanged():
    """Same contract as send: the schema hint is forwarded, never serialized here."""
    response_format = {"type": "object"}
    s = resolved(**{"participant.queue_followup": {"handle": "p-child#4"}})
    await tools.queue_followup(
        s,
        target="p-child",
        prompt="later",
        response_format=response_format,
    )
    assert s.client.params("participant.queue_followup")["response_format"] is response_format


async def test_queue_followup_identifies_first():
    s = session(**{"participant.queue_followup": {"handle": "p-child#4"}})
    await tools.queue_followup(s, target="p-child", prompt="later")
    assert s.client.methods == ["hello", "participant.queue_followup"]


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
            "available": ["send"],
            "unavailable_reasons": {"steer": "wiring_mode"},
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
        (tools.steer_session, {"target": "p-child", "prompt": "amend"}),
        (tools.queue_followup, {"target": "p-child", "prompt": "later"}),
        (tools.update_session_settings, {"target": "p-child", "model": "opus-5"}),
        (tools.get_session_controls, {"target": "p-child"}),
    ):
        with pytest.raises(RemoteError) as exc:
            await call(s, **kwargs)
        assert exc.value.code == "busy"
        assert "is working" in exc.value.message
