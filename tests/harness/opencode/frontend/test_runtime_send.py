"""The OpenCode frontend runtime's send, gating and disconnect behaviour."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping

import pytest

from tests.support.opencode_frontend_events import message_event, status_snapshot
from theater.harness.builtin.plugins.opencode.runtime import OpenCodeFrontendRuntime
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    DeliveryResult,
    NativeTurnTerminal,
    RuntimeCapability,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeFrontendConnection,
    RuntimeIO,
    RuntimeNotification,
    RuntimeRequestError,
    RuntimeRequestTimeout,
)
from theater.models import Status


class _IO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float):
        del endpoint, timeout
        raise AssertionError("OpenCode's frontend runtime never opens a detached backend")


class _Frontend(RuntimeFrontendConnection):
    def __init__(self) -> None:
        self._closed = False
        self._items: asyncio.Queue[RuntimeNotification | None] = asyncio.Queue()

    @property
    def closed(self) -> bool:
        return self._closed

    async def notifications(self) -> AsyncIterator[RuntimeNotification]:
        while True:
            item = await self._items.get()
            if item is None:
                return
            yield item

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._items.put_nowait(None)

    def publish(self, notification: RuntimeNotification) -> None:
        self._items.put_nowait(notification)


def _runtime(frontend: RuntimeFrontendConnection) -> OpenCodeFrontendRuntime:
    return OpenCodeFrontendRuntime(
        RuntimeContext(
            participant_id="open-1",
            cwd="/tmp",
            io=_IO(),
            backend_generation=1,
            endpoint="unix:///tmp/open-1.sock",
            frontend=frontend,
            trusted_session_id_provider=lambda: "ses-1",
        )
    )


class _ScriptedFrontend(_Frontend):
    """A frontend whose single request channel is scripted per test."""

    def __init__(
        self,
        reply: dict[str, object] | None = None,
        error: Exception | None = None,
        before_reply=None,
    ) -> None:
        super().__init__()
        self.requests: list[tuple[str, dict[str, object]]] = []
        self._reply = reply
        self._error = error
        self._before_reply = before_reply

    async def request(
        self, method: str, params: Mapping[str, object], *, timeout: float
    ) -> dict[str, object]:
        assert method == "opencode.send"
        assert timeout > 0
        self.requests.append((method, dict(params)))
        if self._before_reply is not None:
            await self._before_reply()
        if self._error is not None:
            raise self._error
        assert self._reply is not None
        return dict(self._reply)


def _accepted_reply(*, epoch: int = 1, turn_id: str = "msg_abc123def456") -> dict[str, object]:
    return {
        "status": "accepted",
        "operation_id": "op-1",
        "native_session_id": "ses-1",
        "native_turn_id": turn_id,
        "session_epoch": epoch,
    }


async def test_runtime_send_accepts_an_exact_trusted_idle_reply() -> None:
    frontend = _ScriptedFrontend(reply=_accepted_reply())
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    assert (await source.read()).status is Status.IDLE

    receipt = await runtime.send(operation_id="op-1", prompt="hello there")

    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == "msg_abc123def456"
    assert receipt.error_code is None
    assert frontend.requests == [
        (
            "opencode.send",
            {"operation_id": "op-1", "native_session_id": "ses-1", "prompt": "hello there"},
        )
    ]
    snapshot = await runtime.snapshot()
    # The submitted turn is the active lineage target; the TUI's status
    # remains the authoritative execution signal until the turn's lineage
    # arrives (admission is not completion).
    assert snapshot.native_turn_id == "msg_abc123def456"
    assert snapshot.execution_state is RuntimeExecutionState.IDLE

    # Exact assistant lineage completes the submitted turn with evidence.
    frontend.publish(
        message_event(
            {
                "id": "msg_reply_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_abc123def456",
                "time": {"completed": 1500},
            }
        )
    )
    await asyncio.sleep(0.01)
    batch = await source.read()
    assert batch.status is Status.IDLE
    (outcome,) = batch.terminal_evidence
    assert outcome.native_turn_id == "msg_abc123def456"
    assert outcome.terminal is NativeTurnTerminal.COMPLETED
    assert outcome.completed_at == 1.5
    assert source.terminal_evidence_snapshot() == (outcome,)
    source.terminal_evidence_delivered()
    assert source.terminal_evidence_snapshot() == ()
    await runtime.aclose()


@pytest.mark.parametrize(
    "reply",
    [
        {"status": "rejected", "operation_id": "op-1"},
        {
            "status": "accepted",
            "operation_id": "op-2",
            "native_session_id": "ses-1",
            "native_turn_id": "msg_abc123def456",
            "session_epoch": 1,
        },
        {
            "status": "accepted",
            "operation_id": "op-1",
            "native_session_id": "ses-2",
            "native_turn_id": "msg_abc123def456",
            "session_epoch": 1,
        },
        {
            "status": "accepted",
            "operation_id": "op-1",
            "native_session_id": "ses-1",
            "native_turn_id": "msg_abc123def456",
            "session_epoch": 2,
        },
        {
            "status": "accepted",
            "operation_id": "op-1",
            "native_session_id": "ses-1",
            "native_turn_id": "turn_9",
            "session_epoch": 1,
        },
        {"status": "accepted"},
    ],
)
async def test_runtime_send_treats_inexact_success_as_unknown_and_never_replays(reply) -> None:
    frontend = _ScriptedFrontend(reply=reply)
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    await source.read()

    receipt = await runtime.send(operation_id="op-1", prompt="hello there")

    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "delivery_unknown"
    assert frontend.requests == [
        (
            "opencode.send",
            {"operation_id": "op-1", "native_session_id": "ses-1", "prompt": "hello there"},
        )
    ]
    await runtime.aclose()


@pytest.mark.parametrize(
    "error",
    [
        RuntimeRequestTimeout(),
        RuntimeError("socket reset mid-request"),
    ],
)
async def test_runtime_send_maps_transport_failures_to_unknown(error) -> None:
    frontend = _ScriptedFrontend(error=error)
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    await source.read()

    receipt = await runtime.send(operation_id="op-1", prompt="hello there")

    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "delivery_unknown"
    assert len(frontend.requests) == 1
    await runtime.aclose()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("busy", DeliveryResult.REJECTED),
        ("not_ready", DeliveryResult.REJECTED),
        ("wrong_session", DeliveryResult.REJECTED),
        ("operation_in_progress", DeliveryResult.REJECTED),
        ("delivery_unknown", DeliveryResult.UNKNOWN),
        # A server-side code is never a definite client rejection.
        ("native_rejected", DeliveryResult.UNKNOWN),
    ],
)
async def test_runtime_send_maps_plugin_error_codes(code, expected) -> None:
    frontend = _ScriptedFrontend(error=RuntimeRequestError(code, "plugin refused"))
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    await source.read()

    receipt = await runtime.send(operation_id="op-1", prompt="hello there")

    assert receipt.result is expected
    # Definite rejections keep the plugin's code; unknowns use the one
    # wire code for every uncertain delivery.
    assert receipt.error_code == (
        code if expected is DeliveryResult.REJECTED else "delivery_unknown"
    )
    await runtime.aclose()


async def test_runtime_send_gates_on_scope_idleness_and_prompt_shape() -> None:
    frontend = _ScriptedFrontend(reply=_accepted_reply())
    runtime = _runtime(frontend)
    source = runtime.live_source()

    # No visible scope yet: not ready, nothing transmitted.
    receipt = await runtime.send(operation_id="op-1", prompt="hello there")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "not_ready"
    assert frontend.requests == []

    # Visible but busy: Theater keeps the prompt in its own queue.
    frontend.publish(status_snapshot(status="busy"))
    await asyncio.sleep(0.01)
    await source.read()
    receipt = await runtime.send(operation_id="op-1", prompt="hello there")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "busy"
    assert frontend.requests == []

    # Idle but an unusable prompt never reaches the plugin.
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    await source.read()
    receipt = await runtime.send(operation_id="op-1", prompt="")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "invalid_request"
    assert frontend.requests == []
    receipt = await runtime.send(operation_id="op-1", prompt="x" * 60_001)
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "invalid_request"
    assert frontend.requests == []
    # Encoded size, not just char count: a wide prompt never reaches transport.
    receipt = await runtime.send(operation_id="op-1", prompt="\U0001f680" * 20_001)
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "invalid_request"
    assert frontend.requests == []
    await runtime.aclose()


async def test_runtime_send_rejects_frames_the_wire_cannot_carry() -> None:
    frontend = _ScriptedFrontend(reply=_accepted_reply())
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    await source.read()

    # JSON escaping inflates quotes twofold and control characters sixfold;
    # the raw-size checks pass but the frontend frame would be oversized.
    for prompt in ('"' * 40_000, "\x01" * 15_000):
        receipt = await runtime.send(operation_id="op-1", prompt=prompt)
        assert receipt.result is DeliveryResult.REJECTED
        assert receipt.error_code == "invalid_request"
        assert frontend.requests == []

    # A near-bound ordinary prompt still fits and reaches the plugin.
    ordinary = "a" * 59_000
    receipt = await runtime.send(operation_id="op-1", prompt=ordinary)
    assert receipt.result is DeliveryResult.ACCEPTED
    (request,) = frontend.requests
    assert request[1]["prompt"] == ordinary
    await runtime.aclose()


async def test_runtime_send_returns_unknown_when_the_scope_moves_after_the_reply() -> None:
    async def switch_route() -> None:
        frontend.publish(status_snapshot(epoch=2))
        await asyncio.sleep(0.01)

    frontend = _ScriptedFrontend(reply=_accepted_reply(), before_reply=switch_route)
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    await source.read()

    receipt = await runtime.send(operation_id="op-1", prompt="hello there")

    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "delivery_unknown"
    assert receipt.error is not None and "changed while the prompt was in flight" in receipt.error
    assert len(frontend.requests) == 1
    await runtime.aclose()


async def test_runtime_advertises_send_only_while_the_trusted_session_is_connected(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.opencode.live._STATUS_MAX_AGE_SECONDS", 0.05
    )
    frontend = _Frontend()
    runtime = _runtime(frontend)
    source = runtime.live_source()

    snapshot = await runtime.snapshot()
    assert not snapshot.capabilities.supports(RuntimeCapability.SEND)
    assert (
        snapshot.capabilities.reason_for(RuntimeCapability.SEND)
        is CapabilityUnavailableReason.SESSION_STATE
    )

    frontend.publish(status_snapshot(status="busy"))
    await asyncio.sleep(0.01)
    await source.read()
    snapshot = await runtime.snapshot()
    assert snapshot.capabilities.supports(RuntimeCapability.SEND)
    assert snapshot.capabilities.supports(RuntimeCapability.QUEUE_FOLLOWUP)

    # A live scope with a stale status is DEGRADED, never send-capable.
    await asyncio.sleep(0.1)
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DEGRADED
    assert not snapshot.capabilities.supports(RuntimeCapability.SEND)
    assert (
        snapshot.capabilities.reason_for(RuntimeCapability.SEND)
        is CapabilityUnavailableReason.SESSION_STATE
    )

    await frontend.aclose()
    await asyncio.sleep(0.01)
    snapshot = await runtime.snapshot()
    assert not snapshot.capabilities.supports(RuntimeCapability.SEND)
    assert (
        snapshot.capabilities.reason_for(RuntimeCapability.SEND)
        is CapabilityUnavailableReason.GATED_BY_BACKEND
    )
    # Theater policy keeps interrupt, steer and settings out of native space.
    for capability in (
        RuntimeCapability.INTERRUPT,
        RuntimeCapability.STEER,
        RuntimeCapability.SETTINGS_UPDATE,
    ):
        assert (
            snapshot.capabilities.reason_for(capability)
            is CapabilityUnavailableReason.THEATER_POLICY
        )
    await runtime.aclose()


async def test_runtime_send_resolves_a_terminal_lineage_that_beats_the_reply() -> None:
    reply = _accepted_reply()

    async def terminal_before_reply() -> None:
        frontend.publish(
            message_event(
                {
                    "id": "msg_reply_1",
                    "role": "assistant",
                    "sessionID": "ses-1",
                    "parentID": reply["native_turn_id"],
                    "time": {"completed": 1500},
                }
            )
        )
        await asyncio.sleep(0.01)

    frontend = _ScriptedFrontend(reply=reply, before_reply=terminal_before_reply)
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    assert (await source.read()).status is Status.IDLE

    receipt = await runtime.send(operation_id="op-1", prompt="hello there")

    assert receipt.result is DeliveryResult.ACCEPTED
    batch = await source.read()
    (outcome,) = batch.terminal_evidence
    assert outcome.native_turn_id == receipt.native_turn_id
    assert outcome.terminal is NativeTurnTerminal.COMPLETED
    assert outcome.completed_at == 1.5
    assert source.active_turn_id() is None
    source.terminal_evidence_delivered()
    # A repeated event for the same parent never stages a second outcome.
    frontend.publish(
        message_event(
            {
                "id": "msg_reply_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": receipt.native_turn_id,
                "time": {"completed": 1500},
            }
        )
    )
    batch = await source.read()
    assert batch.terminal_evidence == ()
    await runtime.aclose()


async def test_runtime_sends_are_rejected_without_replay_after_disconnect() -> None:
    frontend = _ScriptedFrontend(reply=_accepted_reply())
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(status_snapshot())
    await asyncio.sleep(0.01)
    await source.read()

    await frontend.aclose()
    await asyncio.sleep(0.01)

    receipt = await runtime.send(operation_id="op-1", prompt="hello there")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "not_ready"
    assert frontend.requests == []
    await runtime.aclose()
