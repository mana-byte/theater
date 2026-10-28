"""Pi frontend bridge send binds the durable native turn and never replays uncertain delivery."""

from __future__ import annotations

from tests.support.pi_bridge import (
    SESSION_A,
    ScriptedPiPeer,
    bridge_snapshot,
)
from tests.support.pi_bridge_results import make_runtime
from theater.harness.builtin.plugins.pi.runtime_constants import (
    PI_FRONTEND_SEND_PROMPT_MAX_CHARS,
)
from theater.harness.contracts.runtime import (
    DeliveryResult,
    RuntimeExecutionState,
    RuntimeRequestError,
)


def send_result(
    operation_id: str,
    *,
    session_id: str = SESSION_A,
    native_turn_id: str = "turn-1",
    bridge_epoch: int = 2,
    snapshot_revision: int = 2,
    sequence: int = 1,
    execution_state: str = "active",
) -> dict[str, object]:
    return {
        "status": "accepted",
        "operation_id": operation_id,
        "native_turn_id": native_turn_id,
        **bridge_snapshot(
            session_id=session_id,
            bridge_epoch=bridge_epoch,
            snapshot_revision=snapshot_revision,
            sequence=sequence,
            execution_state=execution_state,
            native_turn_id=native_turn_id,
        ),
    }


async def test_pi_send_refused_admission_is_rejected_and_never_delivered() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(),
            "pi.control.send": RuntimeRequestError("busy", "Pi session is streaming"),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.send(operation_id="send-busy", prompt="hello")
        assert receipt.result is DeliveryResult.REJECTED
        assert receipt.error_code == "busy"
    finally:
        await runtime.aclose()


async def test_pi_send_bounds_the_prompt_before_delivery() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.send(
            operation_id="send-big",
            prompt="x" * (PI_FRONTEND_SEND_PROMPT_MAX_CHARS + 1),
        )
        assert receipt.result is DeliveryResult.REJECTED
        assert receipt.error_code == "prompt_too_large"
        assert all(method != "pi.control.send" for method, _ in peer.requests)
    finally:
        await runtime.aclose()


async def test_pi_send_confirmation_failure_is_unknown_never_replayed() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(),
            # An unaccepted body must never fabricate a delivered turn.
            "pi.control.send": bridge_snapshot(session_id=SESSION_A),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.send(operation_id="send-bad", prompt="hello")
        assert receipt.result is DeliveryResult.UNKNOWN
        assert sum(method == "pi.control.send" for method, _ in peer.requests) == 1
    finally:
        await runtime.aclose()


async def test_pi_send_fast_settle_still_accepts_the_durable_turn_id() -> None:
    # A fast-settling turn already cleared the active turn: the readback is
    # honestly idle, and the receipt must still carry the durable entry id.
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(),
            "pi.control.send": send_result(
                "send-fast", native_turn_id="turn-fast", execution_state="idle"
            ),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.send(operation_id="send-fast", prompt="hello")
        assert receipt.result is DeliveryResult.ACCEPTED
        assert receipt.native_turn_id == "turn-fast"
        method, params = peer.requests[-1]
        assert method == "pi.control.send"
        assert params["native_session_id"] == SESSION_A
        assert (await runtime.snapshot()).native_turn_id == "turn-fast"
        assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    finally:
        await runtime.aclose()


async def test_pi_send_session_drift_after_delivery_is_unknown() -> None:
    # The bridge cannot distinguish pre- from post-delivery session drift,
    # so session_changed must close UNKNOWN — never a confident REJECTED.
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(),
            "pi.control.send": RuntimeRequestError(
                "session_changed", "Pi session changed during the send turn"
            ),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()
    try:
        receipt = await runtime.send(operation_id="send-drift", prompt="hello")
        assert receipt.result is DeliveryResult.UNKNOWN
        assert receipt.error_code == "session_changed"
        assert sum(method == "pi.control.send" for method, _ in peer.requests) == 1
    finally:
        await runtime.aclose()
