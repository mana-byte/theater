"""The OpenCode detached-server runtime: binding, sends, reconnects and the plugin proof gate."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from tests.support.opencode_server import PASSWORD, ServerFake
from tests.support.waiting import wait_until
from theater import paths
from theater.harness.builtin.plugins.opencode import plugin_receipt, server_runtime
from theater.harness.builtin.plugins.opencode.constants import _APPROVAL_SESSION_RULES
from theater.harness.builtin.plugins.opencode.http import (
    OpenCodeHttpError,
)
from theater.harness.builtin.plugins.opencode.mcp import plugin_path
from theater.harness.builtin.plugins.opencode.native_plugin import render_native_plugin
from theater.harness.builtin.plugins.opencode.plugin_receipt import (
    PluginNotLoaded,
    load_receipt_path,
)
from theater.harness.builtin.plugins.opencode.runtime_plan import (
    OPENCODE_SERVER_MAX_VERSION,
    release_text,
)
from theater.harness.builtin.plugins.opencode.server_live import OpenCodeServerLiveSource
from theater.harness.builtin.plugins.opencode.server_plan import (
    OPENCODE_SERVER_COMPATIBILITY_POLICY,
    SERVER_SECRET_ENV,
)
from theater.harness.builtin.plugins.opencode.server_runtime import (
    OpenCodeServerRuntime,
    opencode_server_runtime_factory,
)
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    DeliveryResult,
    RuntimeBinding,
    RuntimeCapability,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeIO,
    RuntimeLifecyclePhase,
    RuntimeSettings,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.models import Status


class _IO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float) -> Any:
        del endpoint, timeout
        raise AssertionError("the server runtime never opens a frontend connection")


def _context(
    server: ServerFake, token_file: Path, *, approval: str | None = None
) -> RuntimeContext:
    return RuntimeContext(
        participant_id="h00000000001",
        cwd="/tmp",
        io=_IO(),
        backend_generation=3,
        endpoint=server.endpoint,
        token_file=token_file,
        approval=approval,
    )


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.token"
    path.write_text(PASSWORD)
    path.chmod(0o600)
    return path


@pytest.fixture
async def server():
    fake = ServerFake()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture(autouse=True)
def fast_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_runtime, "_CONFIRM_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(server_runtime, "_RECONNECT_BACKOFF_SECONDS", 0.02)
    monkeypatch.setattr(server_runtime, "_RECONNECT_MAX_BACKOFF_SECONDS", 0.05)


async def _wait_for_health(
    runtime: OpenCodeServerRuntime, health: ConnectionHealth, timeout: float = 2.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while (await runtime.snapshot()).health is not health:
        assert asyncio.get_running_loop().time() < deadline, "health never settled"
        await asyncio.sleep(0.005)


async def _wait_for_state(
    runtime: OpenCodeServerRuntime, state: RuntimeExecutionState, timeout: float = 2.0
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while (await runtime.snapshot()).execution_state is not state:
        assert asyncio.get_running_loop().time() < deadline, "execution state never settled"
        await asyncio.sleep(0.005)


def _prompts(server: ServerFake) -> list[dict[str, Any]]:
    return [record for record in server.requests if record["path"].endswith("/prompt_async")]


def _prompt_id(server: ServerFake, index: int = -1) -> str:
    parsed = json.loads(_prompts(server)[index]["body"])
    return parsed["messageID"]


def _live(runtime: OpenCodeServerRuntime) -> OpenCodeServerLiveSource:
    source = runtime.live_source()
    assert isinstance(source, OpenCodeServerLiveSource)
    return source


async def _open_new(
    server: ServerFake, token_file: Path
) -> tuple[OpenCodeServerRuntime, RuntimeBinding]:
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    await wait_until(lambda: server.streams_opened >= 1)
    return runtime, binding


def test_the_runtime_requires_endpoint_and_credential(tmp_path: Path) -> None:
    base = {
        "participant_id": "h00000000001",
        "cwd": "/tmp",
        "io": _IO(),
        "backend_generation": 1,
    }
    with pytest.raises(ValueError, match="endpoint"):
        OpenCodeServerRuntime(RuntimeContext(**base))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="credential"):
        OpenCodeServerRuntime(
            RuntimeContext(endpoint="http://127.0.0.1:1", **base)  # type: ignore[arg-type]
        )


def test_the_factory_builds_the_runtime(token_file: Path) -> None:
    runtime = opencode_server_runtime_factory(
        RuntimeContext(
            participant_id="h00000000001",
            cwd="/tmp",
            io=_IO(),
            backend_generation=0,
            endpoint="http://127.0.0.1:1",
            token_file=token_file,
        )
    )
    assert isinstance(runtime, OpenCodeServerRuntime)


async def test_new_session_binding_is_exact_and_idle(server: ServerFake, token_file: Path) -> None:
    runtime, binding = await _open_new(server, token_file)
    assert binding.participant_id == "h00000000001"
    assert binding.backend_generation == 3
    assert binding.wiring is RuntimeWiring.NATIVE
    assert binding.lifecycle is RuntimeLifecyclePhase.BOUND
    assert binding.endpoint == server.endpoint
    assert binding.native_session_id in server.sessions
    assert binding.protocol == "opencode-server-http"
    assert binding.native_version == "1.18.29"
    assert binding.compatibility_policy == OPENCODE_SERVER_COMPATIBILITY_POLICY

    snapshot = await runtime.snapshot()
    assert snapshot.native_session_id == binding.native_session_id
    assert snapshot.execution_state is RuntimeExecutionState.IDLE
    assert snapshot.native_turn_id is None
    await _wait_for_health(runtime, ConnectionHealth.CONNECTED)
    snapshot = await runtime.snapshot()
    assert snapshot.capabilities.available == frozenset(
        {RuntimeCapability.SEND, RuntimeCapability.QUEUE_FOLLOWUP}
    )
    reasons = snapshot.capabilities.unavailable_reasons
    assert reasons[RuntimeCapability.INTERRUPT] is CapabilityUnavailableReason.THEATER_POLICY
    assert reasons[RuntimeCapability.STEER] is CapabilityUnavailableReason.THEATER_POLICY
    assert reasons[RuntimeCapability.SETTINGS_UPDATE] is CapabilityUnavailableReason.THEATER_POLICY
    assert snapshot.settings == RuntimeSettings()
    await runtime.aclose()


async def test_new_session_readback_mismatch_fails_closed(
    server: ServerFake, token_file: Path
) -> None:
    server.behaviors["readback"] = "swap"
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    with pytest.raises(RuntimeError, match="read back a different session"):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.aclose()


async def test_server_handshake_rejects_version_drift(server: ServerFake, token_file: Path) -> None:
    server.behaviors["health-version"] = release_text(OPENCODE_SERVER_MAX_VERSION)
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    with pytest.raises(RuntimeError, match="outside the qualified range"):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.aclose()


async def test_reconnect_establishes_execution_state_from_exact_status(
    server: ServerFake, token_file: Path
) -> None:
    server.add_session("ses_parent")
    for status_type in ("busy", "retry"):
        server.statuses["ses_parent"] = status_type
        runtime = OpenCodeServerRuntime(_context(server, token_file))
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ses_parent")
        assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
        busy = await runtime.send(operation_id="op-1", prompt="hello")
        assert busy.result is DeliveryResult.REJECTED
        assert busy.error_code == "busy"
        await runtime.aclose()

    server.behaviors["status"] = "foreign"
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ses_parent")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    accepted = await runtime.send(operation_id="op-2", prompt="hello")
    assert accepted.result is DeliveryResult.ACCEPTED
    assert len(_prompts(server)) == 1
    await runtime.aclose()

    server.behaviors.pop("status")
    server.statuses.pop("ses_parent", None)
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ses_parent")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    accepted = await runtime.send(operation_id="op-3", prompt="hello")
    assert accepted.result is DeliveryResult.ACCEPTED
    assert len(_prompts(server)) == 2
    await runtime.aclose()


@pytest.mark.parametrize("behavior", ["malformed-top", "malformed-target", "unknown-type", "fail"])
async def test_reconnect_stays_unknown_when_status_cannot_prove_the_session(
    server: ServerFake, token_file: Path, behavior: str
) -> None:
    server.add_session("ses_parent")
    server.behaviors["status"] = behavior
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    binding = await runtime.open_session(
        mode=SessionOpenMode.RECONNECT, native_session_id="ses_parent"
    )
    assert binding.native_session_id == "ses_parent"
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.UNKNOWN
    receipt = await runtime.send(operation_id="op-1", prompt="hello")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "not_ready"
    assert _prompts(server) == []
    await runtime.aclose()


async def test_reconnect_accepts_a_send_once_an_idle_event_proves_the_session(
    server: ServerFake, token_file: Path
) -> None:
    server.add_session("ses_parent")
    server.behaviors["status"] = "fail"
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ses_parent")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.UNKNOWN

    server.push_idle("ses_parent")
    await _wait_for_state(runtime, RuntimeExecutionState.IDLE)

    receipt = await runtime.send(operation_id="op-1", prompt="hello")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == _prompt_id(server)
    await runtime.aclose()


async def test_reconnect_of_a_missing_session_fails_closed(
    server: ServerFake, token_file: Path
) -> None:
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    with pytest.raises(OpenCodeHttpError):
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ses_nope")
    await runtime.aclose()


async def test_fork_binds_the_child_never_the_parent(server: ServerFake, token_file: Path) -> None:
    runtime, parent = await _open_new(server, token_file)
    first = await runtime.send(operation_id="op-3", prompt="say ok")
    assert first.result is DeliveryResult.ACCEPTED
    assert (await runtime.snapshot()).native_turn_id == first.native_turn_id

    child = await runtime.open_session(
        mode=SessionOpenMode.FORK, native_session_id=parent.native_session_id
    )
    assert child.native_session_id != parent.native_session_id
    assert child.native_session_id in server.sessions
    snapshot = await runtime.snapshot()
    assert snapshot.native_session_id == child.native_session_id
    assert snapshot.execution_state is RuntimeExecutionState.IDLE
    assert snapshot.native_turn_id is None
    await runtime.aclose()


async def test_send_accepts_once_with_sse_confirmation(
    server: ServerFake, token_file: Path
) -> None:
    runtime, binding = await _open_new(server, token_file)

    receipt = await runtime.send(operation_id="op-7", prompt="say ok")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.operation_id == "op-7"
    message_id = receipt.native_turn_id
    assert isinstance(message_id, str) and message_id.startswith("msg_")
    prompts = _prompts(server)
    assert len(prompts) == 1
    assert json.loads(prompts[0]["body"]) == {
        "messageID": message_id,
        "parts": [{"type": "text", "text": "say ok"}],
    }
    session_messages = server.messages[binding.native_session_id or ""]
    assert [message["info"]["id"] for message in session_messages] == [message_id]

    snapshot = await runtime.snapshot()
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    assert snapshot.native_turn_id == message_id
    assert _live(runtime).pending_confirmations() == 0

    second = await runtime.send(operation_id="op-8", prompt="again")
    assert second.result is DeliveryResult.REJECTED
    assert second.error_code == "busy"
    assert len(_prompts(server)) == 1
    await runtime.aclose()


async def test_send_accepts_via_readback_when_sse_is_quiet(
    server: ServerFake, token_file: Path
) -> None:
    server.behaviors["prompt"] = "ok-quiet"
    runtime, _ = await _open_new(server, token_file)

    receipt = await runtime.send(operation_id="op-9", prompt="say ok")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == _prompt_id(server)
    assert _live(runtime).pending_confirmations() == 0
    await runtime.aclose()


async def test_send_is_unknown_and_never_replayed_without_admission_evidence(
    server: ServerFake, token_file: Path
) -> None:
    server.behaviors["prompt"] = "ok-no-record"
    runtime, _ = await _open_new(server, token_file)

    receipt = await runtime.send(operation_id="op-10", prompt="say ok")
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "delivery_unknown"
    assert len(_prompts(server)) == 1
    assert _live(runtime).pending_confirmations() == 0
    await runtime.aclose()


async def test_canonical_idle_releases_the_session_for_the_next_send(
    server: ServerFake, token_file: Path
) -> None:
    runtime, binding = await _open_new(server, token_file)
    first = await runtime.send(operation_id="op-20", prompt="say ok")
    assert first.result is DeliveryResult.ACCEPTED
    active = await runtime.snapshot()
    assert active.execution_state is RuntimeExecutionState.ACTIVE
    assert active.native_turn_id == first.native_turn_id

    server.push_idle(binding.native_session_id or "")
    await _wait_for_state(runtime, RuntimeExecutionState.IDLE)
    idle = await runtime.snapshot()
    assert idle.native_turn_id is None

    second = await runtime.send(operation_id="op-21", prompt="again")
    assert second.result is DeliveryResult.ACCEPTED
    assert second.native_turn_id != first.native_turn_id
    assert len(_prompts(server)) == 2
    await runtime.aclose()


async def test_a_fast_completing_turn_never_reverts_to_active(
    server: ServerFake, token_file: Path
) -> None:
    server.behaviors["prompt"] = "fast"
    runtime, _ = await _open_new(server, token_file)

    receipt = await runtime.send(operation_id="op-22", prompt="say ok")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id is not None
    snapshot = await runtime.snapshot()
    assert snapshot.execution_state is RuntimeExecutionState.IDLE
    assert snapshot.native_turn_id is None
    assert _live(runtime).pending_confirmations() == 0

    second = await runtime.send(operation_id="op-23", prompt="again")
    assert second.result is DeliveryResult.ACCEPTED
    assert len(_prompts(server)) == 2
    await runtime.aclose()


async def test_a_stale_idle_does_not_suppress_a_genuinely_active_turn(
    server: ServerFake, token_file: Path
) -> None:
    server.behaviors["prompt"] = "stale-idle"
    runtime, _ = await _open_new(server, token_file)

    receipt = await runtime.send(operation_id="op-24", prompt="say ok")
    assert receipt.result is DeliveryResult.ACCEPTED
    snapshot = await runtime.snapshot()
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    assert snapshot.native_turn_id == receipt.native_turn_id

    second = await runtime.send(operation_id="op-26", prompt="again")
    assert second.result is DeliveryResult.REJECTED
    assert second.error_code == "busy"
    assert len(_prompts(server)) == 1
    await runtime.aclose()


async def test_readback_confirmation_requires_the_exact_user_message(
    server: ServerFake, token_file: Path
) -> None:
    server.behaviors["prompt"] = "ok-wrong-role"
    runtime, _ = await _open_new(server, token_file)

    receipt = await runtime.send(operation_id="op-25", prompt="say ok")
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "delivery_unknown"
    assert len(_prompts(server)) == 1
    assert _live(runtime).pending_confirmations() == 0
    await runtime.aclose()


@pytest.mark.parametrize(
    ("behavior", "expected", "code"),
    [
        ("404", DeliveryResult.REJECTED, "invalid_session"),
        ("400", DeliveryResult.REJECTED, "server_refused"),
        ("500", DeliveryResult.UNKNOWN, "delivery_unknown"),
        ("body", DeliveryResult.UNKNOWN, "delivery_unknown"),
        ("drop", DeliveryResult.UNKNOWN, "delivery_unknown"),
    ],
)
async def test_send_maps_http_answers_exactly(
    server: ServerFake,
    token_file: Path,
    behavior: str,
    expected: DeliveryResult,
    code: str,
) -> None:
    server.behaviors["prompt"] = behavior
    runtime, _ = await _open_new(server, token_file)

    receipt = await runtime.send(operation_id="op-11", prompt="say ok")
    assert receipt.result is expected
    assert receipt.error_code == code
    assert len(_prompts(server)) == 1
    await runtime.aclose()


async def test_send_rejects_bad_requests_without_a_prompt(
    server: ServerFake, token_file: Path
) -> None:
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    for operation_id, prompt in (
        ("op-a", ""),
        ("op-b", "   "),
        ("op-c", "x" * 60_001),
    ):
        receipt = await runtime.send(operation_id=operation_id, prompt=prompt)
        assert receipt.result is DeliveryResult.REJECTED
        assert receipt.error_code == "invalid_request"
    assert _prompts(server) == []

    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id in server.sessions
    await runtime.aclose()


async def test_send_rejects_not_ready_without_a_session_or_a_server(token_file: Path) -> None:
    runtime = OpenCodeServerRuntime(
        RuntimeContext(
            participant_id="h00000000001",
            cwd="/tmp",
            io=_IO(),
            backend_generation=0,
            endpoint="http://127.0.0.1:1",
            token_file=token_file,
        )
    )
    receipt = await runtime.send(operation_id="op-12", prompt="say ok")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "not_ready"
    await runtime.aclose()


async def test_stream_loss_makes_state_unknown_and_reconnect_restores_health(
    server: ServerFake, token_file: Path
) -> None:
    runtime, _ = await _open_new(server, token_file)
    receipt = await runtime.send(operation_id="op-13", prompt="say ok")
    assert receipt.result is DeliveryResult.ACCEPTED

    server.behaviors["health"] = "fail"
    server.close_stream()
    await _wait_for_health(runtime, ConnectionHealth.DEGRADED)
    degraded = await runtime.snapshot()
    assert degraded.execution_state is RuntimeExecutionState.UNKNOWN

    blocked = await runtime.send(operation_id="op-14", prompt="again")
    assert blocked.result is DeliveryResult.REJECTED
    assert blocked.error_code == "not_ready"
    assert len(_prompts(server)) == 1

    server.behaviors.pop("health", None)
    await _wait_for_health(runtime, ConnectionHealth.CONNECTED)
    await wait_until(lambda: server.streams_opened >= 2)
    reconnected = await runtime.snapshot()
    assert reconnected.execution_state is RuntimeExecutionState.ACTIVE
    busy = await runtime.send(operation_id="op-15", prompt="again")
    assert busy.result is DeliveryResult.REJECTED
    assert busy.error_code == "busy"
    assert len(_prompts(server)) == 1

    server.push_idle(reconnected.native_session_id or "")
    await _wait_for_state(runtime, RuntimeExecutionState.IDLE)
    released = await runtime.snapshot()
    assert released.native_turn_id is None
    accepted = await runtime.send(operation_id="op-16", prompt="again")
    assert accepted.result is DeliveryResult.ACCEPTED
    assert len(_prompts(server)) == 2
    await runtime.aclose()


async def test_pending_input_readback_restores_awaiting_after_reconnect(
    server: ServerFake, token_file: Path
) -> None:
    server.add_session("ses_waiting")
    server.statuses["ses_waiting"] = "busy"
    server.inputs["permission"] = [{"id": "per-1", "sessionID": "ses_waiting"}]
    runtime = OpenCodeServerRuntime(_context(server, token_file))
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ses_waiting")
        source = _live(runtime)
        await wait_until(lambda: source.pending_inputs.awaiting)
        assert (await source.read()).status is Status.AWAITING_INPUT
        assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE

        server.inputs["permission"] = []
        server.add_session("ses_child")
        server.sessions["ses_child"]["parentID"] = "ses_waiting"
        server.inputs["question"] = [
            {"id": "q-child", "sessionID": "ses_child"},
            {"id": "q-other", "sessionID": "ses_other"},
        ]
        server.push_event({"type": "question.asked", "properties": server.inputs["question"][0]})
        await wait_until(lambda: source.pending_inputs.accepts("ses_child"))
        server.push_idle("ses_waiting")
        await wait_until(lambda: source.current_execution_state() is RuntimeExecutionState.IDLE)
        assert (await source.read()).status is Status.AWAITING_INPUT
        server.push_event(
            {
                "type": "question.rejected",
                "properties": {
                    "sessionID": "ses_child",
                    "requestID": "q-child",
                },
            }
        )
        await wait_until(lambda: not source.pending_inputs.awaiting)
        assert (await source.read()).status is Status.IDLE

        server.inputs["question"] = [server.inputs["question"][1]]
        server.close_stream()
        await wait_until(lambda: server.streams_opened >= 2)
        await _wait_for_health(runtime, ConnectionHealth.CONNECTED)
        await wait_until(lambda: not source.pending_inputs.awaiting)
        assert (await source.read()).status is Status.IDLE
        assert _prompts(server) == []
        assert all(record["method"] == "GET" for record in server.requests)
    finally:
        await runtime.aclose()


async def test_steer_interrupt_and_settings_updates_are_theater_policy(
    server: ServerFake, token_file: Path
) -> None:
    runtime, _ = await _open_new(server, token_file)

    steer = await runtime.steer(operation_id="op-15", native_turn_id="msg_x", prompt="amend")
    assert steer.result is DeliveryResult.REJECTED
    assert steer.error_code == "theater_policy"

    interrupt = await runtime.interrupt(operation_id="op-16")
    assert interrupt.result is DeliveryResult.REJECTED
    assert interrupt.error_code == "theater_policy"

    settings = await runtime.update_settings(operation_id="op-17", model="mistral/m")
    assert settings.result is DeliveryResult.REJECTED
    assert settings.error_code == "theater_policy"
    await runtime.aclose()


async def test_frontend_plan_builds_the_attach_command(
    server: ServerFake, token_file: Path
) -> None:
    from dataclasses import replace

    runtime, _ = await _open_new(server, token_file)
    binary = token_file.parent / "selected-opencode"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    runtime.context = replace(runtime.context, binary=str(binary))

    with pytest.raises(ValueError, match="session id"):
        await runtime.frontend_plan(native_session_id=None)
    with pytest.raises(ValueError, match="session id"):
        await runtime.frontend_plan(native_session_id="  ")

    plan = await runtime.frontend_plan(native_session_id="ses_attach")
    assert plan.argv == [
        str(binary.resolve()),
        "attach",
        server.endpoint,
        "--session",
        "ses_attach",
    ]
    assert plan.secret_env == {SERVER_SECRET_ENV: token_file}
    await runtime.aclose()


def _enforced_launch_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, approval: str = "manual"
) -> str:
    """Materialize the launch files the runtime reads, as the daemon would."""
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    participant = "h00000000001"
    config_path = paths.mcp_config_path(participant)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("{}")
    plugin = plugin_path(config_path)
    plugin.write_text(
        render_native_plugin(
            participant, config_path.parent / "receipt-token", _APPROVAL_SESSION_RULES[approval]
        )
    )
    return participant


def _write_load_receipt(participant: str, proof: str) -> None:
    receipt = load_receipt_path(participant)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"proof": proof}))


def _rendered_proof(participant: str) -> str:
    source = plugin_path(paths.mcp_config_path(participant)).read_text(encoding="utf-8")
    return source.split('const loadProof = "', 1)[1].split('"', 1)[0]


async def test_manual_refuses_a_session_when_the_plugin_never_proved_it_loaded(
    server: ServerFake, token_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken plugin leaves no receipt, so the session — and with it the
    first prompt — is refused instead of running on the agent's permissions."""
    monkeypatch.setattr(plugin_receipt, "PLUGIN_RECEIPT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(plugin_receipt, "PLUGIN_RECEIPT_POLL_SECONDS", 0.02)
    _enforced_launch_files(tmp_path, monkeypatch)
    runtime = OpenCodeServerRuntime(_context(server, token_file, approval="manual"))
    with pytest.raises(PluginNotLoaded, match="refuses to open one"):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    # No session was adopted, so no prompt_async can ever be submitted.
    assert not _prompts(server)
    receipt = await runtime.send(operation_id="op-proof", prompt="hello")
    assert receipt.result is DeliveryResult.REJECTED
    await runtime.aclose()


async def test_manual_binds_once_the_plugin_proves_it_loaded(
    server: ServerFake, token_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt naming this launch's plugin build is proof enough: the
    session binds (no model call involved in the proof)."""
    participant = _enforced_launch_files(tmp_path, monkeypatch)
    _write_load_receipt(participant, _rendered_proof(participant))
    runtime = OpenCodeServerRuntime(_context(server, token_file, approval="manual"))
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id
    await runtime.aclose()


async def test_reconnecting_a_pre_receipt_participant_is_not_stranded(
    server: ServerFake, token_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plugin rendered before load receipts existed cannot prove anything on a
    daemon restart; refusing would strand its jobs, so only a NEW session needs it."""
    monkeypatch.setattr(plugin_receipt, "PLUGIN_RECEIPT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(plugin_receipt, "PLUGIN_RECEIPT_POLL_SECONDS", 0.02)
    participant = _enforced_launch_files(tmp_path, monkeypatch)
    plugin = plugin_path(paths.mcp_config_path(participant))
    plugin.write_text(
        plugin.read_text(encoding="utf-8").replace(
            f'const loadProof = "{_rendered_proof(participant)}"', "const loadProof = null"
        )
    )
    server.add_session("ses_parent")
    runtime = OpenCodeServerRuntime(_context(server, token_file, approval="manual"))
    binding = await runtime.open_session(
        mode=SessionOpenMode.RECONNECT, native_session_id="ses_parent"
    )
    assert binding.native_session_id == "ses_parent"
    await runtime.aclose()
    runtime = OpenCodeServerRuntime(_context(server, token_file, approval="manual"))
    with pytest.raises(PluginNotLoaded, match="no load proof"):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.aclose()


async def test_a_stale_receipt_from_an_earlier_backend_never_releases_the_gate(
    server: ServerFake, token_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt must name this launch's plugin build; the previous
    backend generation's receipt is not proof that this one loaded."""
    monkeypatch.setattr(plugin_receipt, "PLUGIN_RECEIPT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(plugin_receipt, "PLUGIN_RECEIPT_POLL_SECONDS", 0.02)
    participant = _enforced_launch_files(tmp_path, monkeypatch)
    _write_load_receipt(participant, "0" * 32)
    runtime = OpenCodeServerRuntime(_context(server, token_file, approval="edits"))
    with pytest.raises(PluginNotLoaded):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    assert not _prompts(server)
    await runtime.aclose()


async def test_yolo_never_waits_for_a_plugin_proof(
    server: ServerFake, token_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Yolo enforces nothing, so no plugin file or receipt is required."""
    monkeypatch.setattr(plugin_receipt, "PLUGIN_RECEIPT_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(plugin_receipt, "PLUGIN_RECEIPT_POLL_SECONDS", 0.02)
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    runtime = OpenCodeServerRuntime(_context(server, token_file, approval="yolo"))
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id
    await runtime.aclose()
