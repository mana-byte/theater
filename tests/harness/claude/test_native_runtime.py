"""The Claude bridge uses exact native identity and durable evidence acknowledgements."""

from __future__ import annotations

import asyncio
import time

import pytest

from tests.support.fake_runtime import FakeRuntimeIO, FakeRuntimeState, FakeSource
from tests.support.waiting import wait_until
from theater.harness.builtin.plugins.claude.compatibility import VERSION_POLICY
from theater.harness.builtin.plugins.claude.runtime import ClaudeRuntime
from theater.harness.builtin.plugins.claude.runtime_protocol import (
    CHANNEL_ID,
    PROTOCOL,
    outcome,
    snapshot,
)
from theater.harness.channels.hybrid import HybridSource
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    DeliveryResult,
    LiveChannelDeclaration,
    RuntimeContext,
    RuntimeFrontendConnection,
    RuntimeNotification,
    RuntimeRequestTimeout,
    SessionOpenMode,
)


def _snapshot(**overrides):
    return dict(
        protocol=PROTOCOL,
        session_id="s",
        current_session_id="s",
        generation=1,
        revision=1,
        active_turn_id=None,
        state="idle",
        version=VERSION_POLICY.minimum,
        **overrides,
    )


class _Peer(RuntimeFrontendConnection):
    def __init__(self):
        self._closed = False
        self.state = _snapshot()
        self.events = asyncio.Queue()
        self.calls = []
        self.failure = None
        self.receipt = {}

    @property
    def closed(self):
        return self._closed

    async def request(self, method, params, *, timeout):
        self.calls.append((method, params))
        if self.failure:
            raise self.failure
        if method == "claude.snapshot":
            return dict(self.state)
        if method == "claude.ack":
            return {}
        return {
            "operation_id": params["operation_id"],
            "session_id": "s",
            "generation": 1,
            "result": "accepted",
            "native_turn_id": "turn",
            **self.receipt,
        }

    async def notifications(self):
        while not self.closed:
            yield await self.events.get()

    async def aclose(self):
        self._closed = True


@pytest.fixture
async def runtime():
    peer = _Peer()
    runtime = ClaudeRuntime(
        RuntimeContext(
            participant_id="p",
            cwd="/tmp",
            io=FakeRuntimeIO(FakeRuntimeState("p")),
            backend_generation=1,
            frontend=peer,
            trusted_session_id_provider=lambda: "s",
        )
    )
    await runtime.open_session(mode=SessionOpenMode.RECONNECT)
    yield runtime, peer
    await runtime.aclose()


async def test_native_send_interrupt_and_unsupported_controls(runtime):
    native, peer = runtime
    result = await native.send(operation_id="send", prompt="same text")
    assert result.result is DeliveryResult.ACCEPTED and result.native_turn_id == "turn"
    call = peer.calls[-1]
    assert call == (
        "claude.send",
        {"operation_id": "send", "session_id": "s", "generation": 1, "prompt": "same text"},
    )
    assert (await native.interrupt(operation_id="stop")).result is DeliveryResult.REJECTED
    assert (
        await native.interrupt(operation_id="stop", native_turn_id="turn")
    ).result is DeliveryResult.ACCEPTED
    assert (
        await native.steer(operation_id="steer", native_turn_id="turn", prompt="x")
    ).error_code == "unsupported"
    assert (
        await native.update_settings(operation_id="settings", model="x")
    ).error_code == "unsupported"


async def test_scope_change_removes_controls_without_redirecting(runtime):
    native, peer = runtime
    peer.state.update(current_session_id="other", revision=2)
    result = await native.send(operation_id="no", prompt="x")
    assert result.result is DeliveryResult.REJECTED
    assert native.current_snapshot().health is ConnectionHealth.DEGRADED
    assert not any(method == "claude.send" for method, _ in peer.calls)


async def test_snapshot_failure_recovers_but_uncertain_send_is_not_replayed(runtime):
    native, peer = runtime
    peer.failure = RuntimeRequestTimeout("lost")
    assert (await native.snapshot()).health is ConnectionHealth.DEGRADED
    peer.failure = None
    assert (await native.snapshot()).health is ConnectionHealth.CONNECTED
    original = peer.request

    async def lose(method, params, **kwargs):
        if method == "claude.send":
            peer.calls.append((method, params))
            raise RuntimeRequestTimeout("lost acknowledgement")
        return await original(method, params, **kwargs)

    peer.request = lose
    assert (await native.send(operation_id="once", prompt="x")).result is DeliveryResult.UNKNOWN
    assert sum(method == "claude.send" for method, _ in peer.calls) == 1


async def test_exact_evidence_is_retained_until_durable_acknowledgement(runtime):
    native, peer = runtime
    evidence = {
        "session_id": "s",
        "turn_id": "t",
        "terminal": "completed",
        "result": "DONE",
        "completed_at": time.time() - 10,
    }
    await peer.events.put(RuntimeNotification("history", {"outcomes": [evidence]}))
    await wait_until(native.source.buffered_terminal_evidence, what="native outcome")
    source = HybridSource(
        durable=FakeSource(FakeRuntimeState("p")),
        live=native.source,
        live_channel=LiveChannelDeclaration(
            channel=ChannelDeclaration(id=CHANNEL_ID, kind=ChannelKind.LIVE),
            drives_job_completion=True,
        ),
    )
    batch = await source.read()
    assert batch.terminal_evidence[0].result == "DONE"
    assert batch.terminal_evidence[0].from_history
    assert not any(method == "claude.ack" for method, _ in peer.calls)
    assert (await source.read()).terminal_evidence == batch.terminal_evidence
    source.terminal_evidence_delivered()
    await wait_until(
        lambda: any(m == "claude.ack" for m, _ in peer.calls), what="durable acknowledgement"
    )
    assert not native.source.buffered_terminal_evidence()
    assert peer.calls[-1] == ("claude.ack", {"session_id": "s", "turn_ids": ("t",)})


async def test_foreign_terminal_evidence_closes_the_connection(runtime):
    native, peer = runtime
    await peer.events.put(
        RuntimeNotification(
            "history",
            {
                "outcomes": [
                    {
                        "session_id": "foreign",
                        "turn_id": "t",
                        "terminal": "completed",
                        "result": "BAD",
                        "completed_at": time.time(),
                    }
                ]
            },
        )
    )
    await wait_until(lambda: peer.closed, what="foreign evidence refusal")
    assert not native.source.buffered_terminal_evidence()


@pytest.mark.parametrize(
    "field,value",
    [
        ("generation", True),
        ("protocol", "other"),
        ("state", "ready"),
        ("session_id", ""),
        ("active_turn_id", "not-idle"),
    ],
)
def test_malformed_snapshot_is_never_idle(field, value):
    data = _snapshot()
    data[field] = value
    with pytest.raises((TypeError, ValueError)):
        snapshot(data)


def test_nonfinite_completion_time_is_not_native_evidence():
    with pytest.raises(ValueError, match="completion time"):
        outcome({"completed_at": float("nan")}, connected_at=0)


@pytest.mark.parametrize(
    "key,value",
    [
        ("operation_id", "other"),
        ("session_id", "other"),
        ("generation", 2),
        ("native_turn_id", 17),
    ],
)
async def test_uncorrelated_native_receipt_is_unknown_not_replayed(runtime, key, value):
    native, peer = runtime
    peer.receipt[key] = value
    receipt = await native.send(operation_id="once", prompt="x")
    assert receipt.result is DeliveryResult.UNKNOWN
    assert sum(method == "claude.send" for method, _ in peer.calls) == 1


async def test_stale_snapshot_cannot_replace_newer_active_state(runtime):
    native, peer = runtime
    peer.state.update(revision=3, state="active", active_turn_id="t")
    await native.snapshot()
    peer.state.update(revision=2, state="idle", active_turn_id=None)
    assert (await native.snapshot()).native_turn_id == "t"


async def test_live_source_reports_the_active_native_turn_only_when_connected(runtime):
    native, peer = runtime
    assert native.source.active_native_turn_id() is None
    peer.state.update(revision=2, state="active", active_turn_id="turn-1")
    await native.snapshot()
    assert native.source.active_native_turn_id() == "turn-1"
    peer._closed = True
    assert native.source.active_native_turn_id() is None
