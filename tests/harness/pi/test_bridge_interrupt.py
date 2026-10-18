"""Pi frontend bridge interrupt targets the exact run; post-mutation failure is unknown."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

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


async def test_pi_interrupt_accepts_only_the_exact_identified_run() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
            "pi.control.interrupt": interrupt_result("interrupt-1"),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.interrupt(operation_id="interrupt-1", native_turn_id="turn-1")
        assert receipt.result is DeliveryResult.ACCEPTED
        assert receipt.native_turn_id == "turn-1"
        method, params = peer.requests[-1]
        assert method == "pi.control.interrupt"
        assert params["expected_native_turn_id"] == "turn-1"
        assert params["native_session_id"] == SESSION_A
        assert params["expected_bridge_epoch"] == 2
    finally:
        await runtime.aclose()


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
        assert sum(name == "pi.control.interrupt" for name, _ in peer.requests) == 1
    finally:
        await runtime.aclose()


async def test_pi_interrupt_propagates_only_pre_mutation_rejections() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
            "pi.control.interrupt": RuntimeRequestError(
                "not_cancellable", "Pi run is in retry backoff"
            ),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.interrupt(operation_id="interrupt-backoff", native_turn_id="turn-1")
        assert receipt.result is DeliveryResult.REJECTED
        assert receipt.error_code == "not_cancellable"
        assert sum(method == "pi.control.interrupt" for method, _ in peer.requests) == 1
    finally:
        await runtime.aclose()


async def test_pi_interrupt_post_mutation_failure_is_unknown_never_retried() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
            # A timeout is unordered against the abort: the run may have died.
            "pi.control.interrupt": TimeoutError("evidence window closed"),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.interrupt(operation_id="interrupt-lost", native_turn_id="turn-1")
        assert receipt.result is DeliveryResult.UNKNOWN
        assert sum(method == "pi.control.interrupt" for method, _ in peer.requests) == 1
    finally:
        await runtime.aclose()


async def test_pi_interrupt_turn_identity_mismatch_is_unknown() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
            "pi.control.interrupt": interrupt_result("interrupt-1", native_turn_id="turn-9"),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.interrupt(operation_id="interrupt-echo", native_turn_id="turn-1")
        assert receipt.result is DeliveryResult.UNKNOWN
        assert sum(method == "pi.control.interrupt" for method, _ in peer.requests) == 1
    finally:
        await runtime.aclose()


async def test_pi_interrupt_echo_mismatches_close_unknown() -> None:
    wrong_operation = interrupt_result("interrupt-other")
    wrong_session = interrupt_result("interrupt-echo", session_id=SESSION_B)
    wrong_epoch = {**interrupt_result("interrupt-echo"), "bridge_epoch": 3}
    for reply in (wrong_operation, wrong_session, wrong_epoch):
        peer = ScriptedPiPeer(
            responses={
                "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
                "pi.control.interrupt": reply,
            }
        )
        runtime = make_runtime(peer)
        await runtime.attach()
        try:
            receipt = await runtime.interrupt(
                operation_id="interrupt-echo", native_turn_id="turn-1"
            )
            assert receipt.result is DeliveryResult.UNKNOWN
            assert receipt.error_code == "interrupt_unconfirmed"
        finally:
            await runtime.aclose()


async def test_pi_interrupt_rejects_a_superseded_bridge_epoch() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(execution_state="active", native_turn_id="turn-1"),
            "pi.control.interrupt": RuntimeRequestError(
                "stale_bridge", "the bridge generation is superseded"
            ),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.interrupt(operation_id="interrupt-1", native_turn_id="turn-1")
        assert receipt.result is DeliveryResult.REJECTED
        assert receipt.error_code == "stale_bridge"
        assert sum(name == "pi.control.interrupt" for name, _ in peer.requests) == 1
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


async def test_pi_snapshot_without_interrupt_capability_decodes_compatibility() -> None:
    partial = bridge_snapshot()
    capabilities = partial["capabilities"]
    assert isinstance(capabilities, dict)
    del capabilities["interrupt"]
    peer = ScriptedPiPeer(responses={"pi.snapshot": partial})
    runtime = make_runtime(peer)

    snapshot = await runtime.attach()

    assert snapshot.capabilities.supports(RuntimeCapability.INTERRUPT) is False
    assert (
        snapshot.capabilities.unavailable_reasons[RuntimeCapability.INTERRUPT]
        is CapabilityUnavailableReason.GATED_BY_BACKEND
    )
    await runtime.aclose()


async def test_pi_interrupt_requires_a_backend_confirmed_capability() -> None:
    peer = ScriptedPiPeer(
        responses={
            # An older bridge never advertises the additive interrupt key.
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
