"""Pi frontend bridge interrupt targets the exact run; post-mutation failure is unknown."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from tests.support.pi_bridge import (
    SESSION_A,
    SESSION_B,
    ScriptedPiPeer,
    bridge_snapshot,
)
from tests.support.pi_bridge_results import eventually, make_runtime
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    DeliveryResult,
    RuntimeCapability,
    RuntimeRequestError,
)


def interrupt_result(
    operation_id: str,
    *,
    session_id: str = SESSION_A,
    native_turn_id: str | None = "turn-1",
    execution_state: str = "idle",
) -> dict[str, object]:
    return {
        "status": "accepted",
        "operation_id": operation_id,
        **bridge_snapshot(
            session_id=session_id,
            native_turn_id=native_turn_id,
            execution_state=execution_state,
        ),
    }


async def test_pi_interrupt_scope_is_the_bridges_atomic_check() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
            "pi.control.interrupt": RuntimeRequestError(
                "stale_turn", "not the identified active run"
            ),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        missing = await runtime.interrupt(operation_id="interrupt-none")
        assert missing.result is DeliveryResult.REJECTED
        assert missing.error_code == "invalid_request"
        stale = await runtime.interrupt(operation_id="interrupt-stale", native_turn_id="turn-other")
        assert stale.result is DeliveryResult.REJECTED
        assert stale.error_code == "stale_turn"
        method, params = peer.requests[-1]
        assert method == "pi.control.interrupt"
        assert params["expected_native_turn_id"] == "turn-other"
        assert params["native_session_id"] == SESSION_A
        assert params["expected_bridge_epoch"] == 2
        assert sum(name == "pi.control.interrupt" for name, _ in peer.requests) == 1
    finally:
        await runtime.aclose()


_REJECTED = DeliveryResult.REJECTED
_UNKNOWN = DeliveryResult.UNKNOWN


@pytest.mark.parametrize(
    ("operation_id", "reply", "result", "error_code"),
    [
        pytest.param(
            "interrupt-backoff",
            RuntimeRequestError("not_cancellable", "Pi run is in retry backoff"),
            _REJECTED,
            "not_cancellable",
            id="pre-mutation-rejection",
        ),
        pytest.param(
            "interrupt-1",
            RuntimeRequestError("stale_bridge", "the bridge generation is superseded"),
            _REJECTED,
            "stale_bridge",
            id="superseded-bridge-epoch",
        ),
        # A timeout is unordered against the abort: the run may have died.
        pytest.param(
            "interrupt-lost",
            TimeoutError("evidence window closed"),
            _UNKNOWN,
            None,
            id="post-mutation-failure",
        ),
        pytest.param(
            "interrupt-echo",
            interrupt_result("interrupt-1", native_turn_id="turn-9"),
            _UNKNOWN,
            None,
            id="turn-identity-mismatch",
        ),
        pytest.param(
            "interrupt-echo",
            interrupt_result("interrupt-other"),
            _UNKNOWN,
            "interrupt_unconfirmed",
            id="echo-wrong-operation",
        ),
        pytest.param(
            "interrupt-echo",
            interrupt_result("interrupt-echo", session_id=SESSION_B),
            _UNKNOWN,
            "interrupt_unconfirmed",
            id="echo-wrong-session",
        ),
        pytest.param(
            "interrupt-echo",
            {**interrupt_result("interrupt-echo"), "bridge_epoch": 3},
            _UNKNOWN,
            "interrupt_unconfirmed",
            id="echo-wrong-epoch",
        ),
    ],
)
async def test_pi_interrupt_failures_are_classified_and_never_retried(
    operation_id, reply, result, error_code
) -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
            "pi.control.interrupt": reply,
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.interrupt(operation_id=operation_id, native_turn_id="turn-1")
        assert receipt.result is result
        if error_code is not None:
            assert receipt.error_code == error_code
        assert sum(method == "pi.control.interrupt" for method, _ in peer.requests) == 1
    finally:
        await runtime.aclose()


async def test_pi_interrupt_in_flight_replacement_is_unknown_not_replayed() -> None:
    gate = asyncio.Event()

    async def hang_interrupt(params: Mapping[str, object]) -> object:
        del params
        await gate.wait()
        return interrupt_result("interrupt-1")

    old_peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
            "pi.control.interrupt": hang_interrupt,
        }
    )
    runtime = make_runtime(old_peer)
    await runtime.attach()
    try:
        in_flight = asyncio.create_task(
            runtime.interrupt(operation_id="interrupt-1", native_turn_id="turn-1")
        )
        await eventually(
            lambda: any(name == "pi.control.interrupt" for name, _ in old_peer.requests)
        )
        new_peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
        await runtime.reconnect(new_peer)
        gate.set()
        receipt = await in_flight
        assert receipt.result is DeliveryResult.UNKNOWN
        assert receipt.error_code == "session_changed"
        assert sum(name == "pi.control.interrupt" for name, _ in old_peer.requests) == 1
        assert all(name != "pi.control.interrupt" for name, _ in new_peer.requests)
    finally:
        await runtime.aclose()


async def test_pi_interrupt_requires_a_backend_confirmed_capability() -> None:
    peer = ScriptedPiPeer(
        responses={
            # The bridge explicitly denies interrupt; capability is never assumed.
            "pi.snapshot": bridge_snapshot(interrupt=False),
        }
    )
    runtime = make_runtime(peer)
    snapshot = await runtime.attach()
    try:
        assert not snapshot.capabilities.supports(RuntimeCapability.INTERRUPT)
        assert (
            snapshot.capabilities.unavailable_reasons[RuntimeCapability.INTERRUPT]
            is CapabilityUnavailableReason.GATED_BY_BACKEND
        )
    finally:
        await runtime.aclose()
