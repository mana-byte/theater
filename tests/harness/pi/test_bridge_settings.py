"""Pi frontend bridge settings: confirmed read-back, gating and the trusted session."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from tests.support.pi_bridge import (
    GENERATION,
    PARTICIPANT,
    SESSION_A,
    SESSION_B,
    ScriptedPiPeer,
    bridge_snapshot,
)
from tests.support.pi_bridge_results import eventually, make_runtime
from theater.harness.builtin.plugins.pi.runtime import PiFrontendRuntime
from theater.harness.contracts.runtime import (
    DeliveryResult,
    RuntimeCapability,
    RuntimeExecutionState,
    RuntimeRequestError,
    RuntimeRequestTimeout,
    RuntimeSettingField,
)
from theater.models import Status


def settings_result(
    operation_id: str,
    *,
    session_id: str = SESSION_A,
    bridge_epoch: int = 2,
    snapshot_revision: int = 2,
    sequence: int = 1,
    model: str | None = "openai/gpt-5.6",
    thinking: str | None = "high",
    execution_state: str = "idle",
) -> dict[str, object]:
    return {
        "status": "accepted",
        "operation_id": operation_id,
        **bridge_snapshot(
            session_id=session_id,
            bridge_epoch=bridge_epoch,
            snapshot_revision=snapshot_revision,
            sequence=sequence,
            model=model,
            thinking=thinking,
            execution_state=execution_state,
        ),
    }


async def test_pi_frontend_attaches_and_exposes_only_confirmed_settings() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)

    snapshot = await runtime.attach()

    assert snapshot.native_session_id == SESSION_A
    assert snapshot.execution_state is RuntimeExecutionState.IDLE
    assert snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE)
    assert snapshot.capabilities.supports(RuntimeCapability.SEND)
    assert snapshot.capabilities.supports(RuntimeCapability.INTERRUPT)
    assert (
        await runtime.steer(operation_id="op-steer", native_turn_id="turn-1", prompt="hello")
    ).error_code == "native_control_proof_gated"
    assert (await runtime.interrupt(operation_id="op-interrupt")).error_code == "invalid_request"

    source = runtime.live_source()
    batch = await source.read()
    assert batch.status is Status.IDLE
    assert not batch.terminal_evidence
    await runtime.aclose()


async def test_pi_frontend_settings_confirm_thinking_readback() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})

    def update(params: Mapping[str, object]) -> Mapping[str, object]:
        assert params == {
            "operation_id": "settings-1",
            "native_session_id": SESSION_A,
            "reasoning_effort": "max",
        }
        response = settings_result("settings-1", thinking="high")
        peer.responses["pi.snapshot"] = bridge_snapshot(
            thinking="high", snapshot_revision=3, sequence=1
        )
        return response

    peer.responses["pi.settings.update"] = update
    runtime = make_runtime(peer)
    await runtime.attach()

    receipt = await runtime.update_settings(
        operation_id="settings-1",
        reasoning_effort="max",
    )
    snapshot = await runtime.snapshot()

    assert receipt.result is DeliveryResult.ACCEPTED
    assert snapshot.settings.model == "openai/gpt-5.6"
    # The bridge returns the effective Pi value rather than pretending the
    # requested but clamped level survived unchanged.
    assert snapshot.settings.reasoning_effort == "high"
    assert snapshot.settings.supported_fields == frozenset({RuntimeSettingField.REASONING_EFFORT})
    await runtime.aclose()


async def test_pi_frontend_refuses_busy_model_gated_or_definitively_rejected_settings() -> None:
    busy_peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot(execution_state="active")})
    busy_runtime = make_runtime(busy_peer)
    await busy_runtime.attach()

    busy = await busy_runtime.update_settings(operation_id="busy-1", reasoning_effort="high")

    assert busy.result is DeliveryResult.REJECTED
    assert busy.error_code == "settings_not_idle"
    assert [method for method, _ in busy_peer.requests] == ["pi.snapshot", "pi.snapshot"]
    await busy_runtime.aclose()

    model_peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    model_runtime = make_runtime(model_peer)
    await model_runtime.attach()

    model = await model_runtime.update_settings(
        operation_id="model-1", model="openai/not-scoped", reasoning_effort="high"
    )

    assert model.result is DeliveryResult.REJECTED
    assert model.error_code == "model_update_proof_gated"
    assert [method for method, _ in model_peer.requests] == ["pi.snapshot"]
    await model_runtime.aclose()

    rejected_peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(),
            "pi.settings.update": RuntimeRequestError(
                "unsupported_thinking", "thinking is unavailable"
            ),
        }
    )
    rejected_runtime = make_runtime(rejected_peer)
    await rejected_runtime.attach()

    rejected = await rejected_runtime.update_settings(
        operation_id="thinking-1", reasoning_effort="max"
    )

    assert rejected.result is DeliveryResult.REJECTED
    assert rejected.error_code == "unsupported_thinking"
    await rejected_runtime.aclose()

    wrong_session_peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(),
            "pi.settings.update": RuntimeRequestError("wrong_session", "native session changed"),
        }
    )
    wrong_session_runtime = make_runtime(wrong_session_peer)
    await wrong_session_runtime.attach()

    wrong_session = await wrong_session_runtime.update_settings(
        operation_id="wrong-session-1", reasoning_effort="high"
    )

    assert wrong_session.result is DeliveryResult.REJECTED
    assert wrong_session.error_code == "wrong_session"
    await wrong_session_runtime.aclose()


async def test_pi_frontend_never_replays_uncertain_settings_delivery() -> None:
    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(),
            "pi.settings.update": RuntimeRequestTimeout("lost response"),
        }
    )
    runtime = make_runtime(peer)
    await runtime.attach()

    receipt = await runtime.update_settings(operation_id="timeout-1", reasoning_effort="high")

    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "settings_delivery_unknown"
    assert [method for method, _ in peer.requests].count("pi.settings.update") == 1
    health = runtime.live_source().health_snapshot()[0]
    assert health.state.value == "failed"
    await runtime.aclose()


async def test_pi_frontend_session_switch_invalidates_inflight_settings() -> None:
    gate = asyncio.Event()
    entered = asyncio.Event()
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})

    async def update(params: Mapping[str, object]) -> Mapping[str, object]:
        assert params["native_session_id"] == SESSION_A
        entered.set()
        await gate.wait()
        return settings_result("switch-1", session_id=SESSION_A, thinking="max")

    peer.responses["pi.settings.update"] = update
    runtime = make_runtime(peer)
    await runtime.attach()
    task = asyncio.create_task(
        runtime.update_settings(operation_id="switch-1", reasoning_effort="max")
    )
    await entered.wait()
    peer.push(
        {"type": "snapshot", "snapshot": bridge_snapshot(session_id=SESSION_B, bridge_epoch=3)}
    )
    await eventually(lambda: runtime._native_session_id == SESSION_B)
    gate.set()
    receipt = await task

    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "session_changed"
    assert [method for method, _ in peer.requests].count("pi.settings.update") == 1
    await runtime.aclose()


async def test_pi_settings_require_the_daemons_current_trusted_session():
    trusted = [SESSION_A]
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = PiFrontendRuntime(
        participant_id=PARTICIPANT,
        backend_generation=GENERATION,
        peer=peer,
        trusted_session_id_provider=lambda: trusted[0],
    )
    await runtime.attach()
    trusted[0] = SESSION_B
    try:
        receipt = await runtime.update_settings(
            operation_id="wrong-session", reasoning_effort="low"
        )
        assert receipt.result is DeliveryResult.REJECTED
        assert all(method == "pi.snapshot" for method, _ in peer.requests)
        assert not (await runtime.snapshot()).capabilities.supports(
            RuntimeCapability.SETTINGS_UPDATE
        )
    finally:
        await runtime.aclose()


async def test_pi_settings_become_unknown_if_trusted_identity_changes_during_reply():
    trusted = [SESSION_A]

    def mutate_and_switch(params):
        trusted[0] = SESSION_B
        return settings_result(params["operation_id"], thinking="low")

    peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(),
            "pi.settings.update": mutate_and_switch,
        }
    )
    runtime = PiFrontendRuntime(
        participant_id=PARTICIPANT,
        backend_generation=GENERATION,
        peer=peer,
        trusted_session_id_provider=lambda: trusted[0],
    )
    await runtime.attach()
    try:
        receipt = await runtime.update_settings(
            operation_id="switch-in-flight", reasoning_effort="low"
        )
        assert receipt.result is DeliveryResult.UNKNOWN
        assert sum(method == "pi.settings.update" for method, _ in peer.requests) == 1
    finally:
        await runtime.aclose()
