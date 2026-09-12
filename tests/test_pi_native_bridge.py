"""Focused contract tests for Pi's stock-extension frontend bridge seam."""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from theater.harness.builtin.plugins.pi import runtime as pi_runtime_module
from theater.harness.builtin.plugins.pi.frontend import (
    PI_FRONTEND_PROTOCOL,
    PiFrontendBridgeConfig,
    with_frontend_bridge,
)
from theater.harness.builtin.plugins.pi.launch import plan_launch
from theater.harness.builtin.plugins.pi.runtime import PiFrontendPeer, PiFrontendRuntime
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.runtime import (
    DeliveryResult,
    RuntimeCapability,
    RuntimeExecutionState,
    RuntimeRequestError,
    RuntimeRequestTimeout,
)
from theater.models import Status

PARTICIPANT = "pi-bridge-child"
GENERATION = 4
SESSION_A = "pi-session-a"
SESSION_B = "pi-session-b"


def bridge_snapshot(
    *,
    session_id: str = SESSION_A,
    bridge_epoch: int = 2,
    snapshot_revision: int = 1,
    sequence: int = 0,
    model: str | None = "openai/gpt-5.6",
    thinking: str | None = "high",
    execution_state: str = "idle",
    settings_update: bool = True,
) -> dict[str, object]:
    return {
        "protocol": PI_FRONTEND_PROTOCOL,
        "native_session_id": session_id,
        "bridge_epoch": bridge_epoch,
        "snapshot_revision": snapshot_revision,
        "sequence": sequence,
        "settings": {"model": model, "reasoning_effort": thinking},
        "execution_state": execution_state,
        "capabilities": {
            "settings_update": settings_update,
            "model_update": False,
            "reasoning_effort_update": True,
        },
    }


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


ResponseHandler = Callable[[Mapping[str, object]], object | Awaitable[object]]


@dataclass
class ScriptedPiPeer:
    responses: dict[str, object | ResponseHandler] = field(default_factory=dict)
    requests: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    notifications_queue: asyncio.Queue[object] = field(default_factory=asyncio.Queue)
    closed: bool = False

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        del timeout
        self.requests.append((method, dict(params)))
        response = self.responses.get(method)
        if response is None:
            raise AssertionError(f"unexpected Pi frontend request: {method}")
        value = response(params) if callable(response) else response
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, Exception):
            raise value
        assert isinstance(value, Mapping)
        return dict(value)

    def notifications(self) -> AsyncIterator[Mapping[str, object]]:
        return self._notifications()

    async def _notifications(self) -> AsyncIterator[Mapping[str, object]]:
        while not self.closed:
            frame = await self.notifications_queue.get()
            if frame is None:
                return
            assert isinstance(frame, Mapping)
            yield cast(Mapping[str, object], frame)

    async def aclose(self) -> None:
        self.closed = True
        self.notifications_queue.put_nowait(None)

    def push(self, frame: Mapping[str, object]) -> None:
        self.notifications_queue.put_nowait(dict(frame))


def make_runtime(peer: PiFrontendPeer) -> PiFrontendRuntime:
    return PiFrontendRuntime(
        participant_id=PARTICIPANT,
        backend_generation=GENERATION,
        peer=peer,
    )


async def eventually(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


async def test_pi_frontend_attaches_and_exposes_only_confirmed_settings() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)

    snapshot = await runtime.attach()

    assert snapshot.native_session_id == SESSION_A
    assert snapshot.execution_state is RuntimeExecutionState.IDLE
    assert snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE)
    assert not snapshot.capabilities.supports(RuntimeCapability.SEND)
    assert (
        await runtime.send(operation_id="op-send", prompt="hello")
    ).result is DeliveryResult.REJECTED
    assert (
        await runtime.steer(operation_id="op-steer", native_turn_id="turn-1", prompt="hello")
    ).error_code == "native_control_proof_gated"
    assert (
        await runtime.interrupt(operation_id="op-interrupt")
    ).error_code == "native_control_proof_gated"

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


async def test_pi_frontend_agent_end_stays_working_until_agent_settled() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()
    source = runtime.live_source()
    await source.read()

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_start",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "active",
                "sequence": 1,
            },
        }
    )
    await eventually(lambda: runtime._execution_state is RuntimeExecutionState.ACTIVE)
    assert (await source.read()).status is Status.WORKING

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_end",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "active",
                "sequence": 2,
            },
        }
    )
    await asyncio.sleep(0)
    assert (await source.read()).status is Status.WORKING

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_settled",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "idle",
                "sequence": 3,
            },
        }
    )
    await eventually(lambda: runtime._execution_state is RuntimeExecutionState.IDLE)
    assert (await source.read()).status is Status.IDLE
    await runtime.aclose()


async def test_pi_frontend_ignores_old_session_settled_after_reload() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "session_shutdown",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "unknown",
                "sequence": 1,
            },
        }
    )
    await eventually(lambda: runtime._native_session_id is None)
    peer.push(
        {"type": "snapshot", "snapshot": bridge_snapshot(session_id=SESSION_B, bridge_epoch=3)}
    )
    await eventually(lambda: runtime._native_session_id == SESSION_B)
    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_settled",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "idle",
                "sequence": 2,
            },
        }
    )
    await asyncio.sleep(0)

    assert runtime._native_session_id == SESSION_B
    assert runtime._execution_state is RuntimeExecutionState.IDLE
    await runtime.aclose()


async def test_pi_frontend_delayed_old_history_cannot_roll_back_identity_or_idle() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()

    peer.push(
        {
            "type": "snapshot",
            "snapshot": bridge_snapshot(
                session_id=SESSION_B,
                bridge_epoch=3,
                model="anthropic/claude-test",
                thinking="low",
                execution_state="active",
            ),
        }
    )
    await eventually(lambda: runtime._native_session_id == SESSION_B)

    # A reconnect can deliver the prior socket's buffered history after the
    # successor snapshot.  Neither its session_start nor settled boundary may
    # re-establish A, re-enable old settings, or manufacture idle for B.
    peer.push(
        {
            "type": "history",
            "snapshot": bridge_snapshot(session_id=SESSION_A, bridge_epoch=2),
            "events": [
                {
                    "name": "session_start",
                    "native_session_id": SESSION_A,
                    "bridge_epoch": 2,
                    "execution_state": "idle",
                    "sequence": 0,
                },
                {
                    "name": "agent_settled",
                    "native_session_id": SESSION_A,
                    "bridge_epoch": 2,
                    "execution_state": "idle",
                    "sequence": 9,
                },
            ],
        }
    )
    await asyncio.sleep(0)

    assert runtime._native_session_id == SESSION_B
    assert runtime._bridge_epoch == 3
    assert runtime._execution_state is RuntimeExecutionState.ACTIVE
    assert runtime._settings.model == "anthropic/claude-test"
    assert runtime.live_source().health_snapshot()[0].dropped >= 1
    await runtime.aclose()


async def test_pi_frontend_rejects_same_epoch_conflicting_snapshot_and_event_identity() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()

    # Events do not establish identity, including a seemingly newer
    # session_start.  Only a snapshot with a strictly newer bridge epoch may.
    peer.push(
        {
            "type": "event",
            "event": {
                "name": "session_start",
                "native_session_id": SESSION_B,
                "bridge_epoch": 3,
                "execution_state": "idle",
                "sequence": 0,
            },
        }
    )
    peer.push(
        {
            "type": "snapshot",
            "snapshot": bridge_snapshot(session_id=SESSION_B, bridge_epoch=2),
        }
    )
    await asyncio.sleep(0)

    assert runtime._native_session_id == SESSION_A
    assert runtime._bridge_epoch == 2
    assert runtime._execution_state is RuntimeExecutionState.IDLE
    await runtime.aclose()


async def test_pi_frontend_delayed_same_session_history_cannot_regress_idle() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_start",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "active",
                "sequence": 1,
            },
        }
    )
    peer.push(
        {
            "type": "snapshot",
            "snapshot": bridge_snapshot(
                snapshot_revision=2,
                sequence=1,
                execution_state="active",
            ),
        }
    )
    await eventually(lambda: runtime._execution_state is RuntimeExecutionState.ACTIVE)

    # An old reconnect history for the same session/epoch is still stale.  Its
    # lower snapshot revision and sequence cannot turn a known working run idle.
    peer.push(
        {
            "type": "history",
            "snapshot": bridge_snapshot(
                snapshot_revision=1,
                sequence=0,
                execution_state="idle",
            ),
            "events": [
                {
                    "name": "agent_settled",
                    "native_session_id": SESSION_A,
                    "bridge_epoch": 2,
                    "execution_state": "idle",
                    "sequence": 0,
                }
            ],
        }
    )
    await asyncio.sleep(0)

    assert runtime._execution_state is RuntimeExecutionState.ACTIVE
    assert runtime._snapshot_revision == 2
    assert runtime._last_sequence == 1
    await runtime.aclose()


async def test_pi_frontend_explicit_peer_reconnect_is_the_only_lower_epoch_reset() -> None:
    old_peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(
                bridge_epoch=8,
                snapshot_revision=9,
                sequence=12,
            )
        }
    )
    runtime = make_runtime(old_peer)
    await runtime.attach()

    replacement = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(
                session_id=SESSION_B,
                bridge_epoch=1,
                snapshot_revision=1,
                sequence=0,
            )
        }
    )
    snapshot = await runtime.reconnect(replacement)

    assert old_peer.closed is True
    assert snapshot.native_session_id == SESSION_B
    assert runtime._bridge_epoch == 1
    assert runtime._snapshot_revision == 1
    assert runtime._last_sequence == 0
    await runtime.aclose()


def test_pi_frontend_bridge_overlay_keeps_token_out_of_argv_and_public_files(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    plan = plan_launch(LaunchContext(PARTICIPANT, "inspect", tmp_path / "mcp.json", "yolo"))
    token = "token-do-not-put-in-argv-0123456789"

    bridged = with_frontend_bridge(
        plan,
        PiFrontendBridgeConfig(
            participant_id=PARTICIPANT,
            endpoint="tcp://127.0.0.1:43123",
            token=token,
        ),
    )

    assert "--theater-frontend-config" not in plan.argv
    assert "--theater-frontend-config" in bridged.argv
    assert all(token not in value for value in bridged.argv)
    assert all(token not in value for value in bridged.files.values())
    assert len(bridged.private_files) == 1
    private = json.loads(next(iter(bridged.private_files.values())))
    assert private == {
        "endpoint": "tcp://127.0.0.1:43123",
        "participant_id": PARTICIPANT,
        "protocol": PI_FRONTEND_PROTOCOL,
        "token": token,
    }


@dataclass
class _Completed:
    stdout: str
    stderr: str = ""
    returncode: int = 0


def test_pi_frontend_probe_accepts_supported_range_without_mutating_help_probe(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def run(argv, **kwargs):
        del kwargs
        calls.append(tuple(argv))
        assert argv[1] == "--version"
        return _Completed("0.84.9\n")

    monkeypatch.setattr(pi_runtime_module.subprocess, "run", run)

    compatibility = pi_runtime_module.probe_pi_frontend_compatibility(
        pi_runtime_module.RuntimeProbeContext(binary="pi")
    )

    assert compatibility.supported is True
    assert compatibility.native_version == "0.84.9"
    assert calls == [("pi", "--version")]

    def outside_range(argv, **kwargs):
        del argv, kwargs
        return _Completed("0.85.0\n")

    monkeypatch.setattr(pi_runtime_module.subprocess, "run", outside_range)
    refused = pi_runtime_module.probe_pi_frontend_compatibility(
        pi_runtime_module.RuntimeProbeContext(binary="pi")
    )
    assert refused.supported is False
    assert refused.native_version == "0.85.0"


def test_pi_extension_uses_public_lifecycle_and_settings_surfaces_only() -> None:
    bridge = (
        Path(__file__).parents[1] / "theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts"
    ).read_text(encoding="utf-8")

    assert "AgentSession" not in bridge
    assert 'pi.on("agent_settled"' in bridge
    assert 'pi.on("agent_end"' in bridge
    assert "model_update_proof_gated" in bridge
    assert "await this.pi.setModel(" not in bridge
    assert "this.pi.getThinkingLevel()" in bridge
    assert "this.pi.setThinkingLevel(" in bridge
    assert "--theater-frontend-config" in bridge
    assert "FRONTEND_OWNER" in bridge
    assert "bridge_epoch" in bridge
    assert 'pi.on("session_before_compact"' in bridge
    assert 'bridge.transition(ctx, "agent_end", "active")' in bridge


def test_pi_extension_frontend_bridge_executable_conformance() -> None:
    """Drive the actual extension registration through fresh public contexts."""
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            "node",
            "--experimental-transform-types",
            "tests/fixtures/pi_frontend_bridge_conformance.mts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi frontend bridge executable conformance: ok" in result.stdout
