"""Passive Pi/OpenCode frontends: duplex requests, authenticated listener and spawn fallback."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.harness_runtime.constants import FRONTEND_QUEUE_MAX
from theater.daemon.harness_runtime.errors import RuntimePayloadTooLarge
from theater.daemon.harness_runtime.frontend import (
    FrontendProtocolError,
    FrontendRuntimeHost,
    UnixFrontendConnection,
)
from theater.daemon.harness_runtime.frontend_requests import FrontendRequests
from theater.daemon.runtime.wiring import frontend_endpoint
from theater.daemon.server import Daemon
from theater.daemon.spawning.models import SpawnRequest
from theater.daemon.spawning.provider_launch import ParticipantLaunchService
from theater.harness import HARNESSES, Harness
from theater.harness.builtin.plugins.opencode.runtime import (
    OpenCodeFrontendRuntime,
    opencode_frontend_runtime_factory,
)
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.harness import LaunchParameterSupport
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    ControlDeliveryPhase,
    ControlTransport,
    LiveChannelDeclaration,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeCompatibility,
    RuntimeConnectionClosed,
    RuntimeExecutionState,
    RuntimeFrontendConnection,
    RuntimeFrontendOverlay,
    RuntimeHost,
    RuntimeLifecyclePhase,
    RuntimeManifest,
    RuntimeNotification,
    RuntimeRequestError,
    RuntimeRequestTimeout,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.harness.observation import TranscriptObserver
from theater.models import Status


@pytest.mark.asyncio
async def test_frontend_requests_correlate_out_of_order_and_remote_errors():
    sent = asyncio.Queue()

    async def send(payload):
        sent.put_nowait(json.loads(payload))

    requests = FrontendRequests(send)
    first = asyncio.create_task(requests.request("pi.snapshot", {}, timeout=1))
    second = asyncio.create_task(requests.request("pi.settings.update", {}, timeout=1))
    first_frame, second_frame = await sent.get(), await sent.get()
    assert first_frame["id"] != second_frame["id"]
    requests.receive(
        {
            "type": "response",
            "id": second_frame["id"],
            "error": {"code": "busy", "message": "not idle"},
        }
    )
    requests.receive({"type": "response", "id": first_frame["id"], "result": {"session": "a"}})
    assert await first == {"session": "a"}
    with pytest.raises(RuntimeRequestError) as error:
        await second
    assert error.value.code == "busy"
    requests.close()


@pytest.mark.asyncio
async def test_frontend_request_timeout_discards_late_reply_without_replay():
    sent = []

    async def send(payload):
        sent.append(json.loads(payload))

    requests = FrontendRequests(send)
    with pytest.raises(RuntimeRequestTimeout):
        await requests.request("pi.settings.update", {"operation_id": "once"}, timeout=0.01)
    assert len(sent) == 1
    assert not requests.receive({"type": "response", "id": sent[0]["id"], "result": {}})
    assert requests._pending == {}
    requests.close()


@pytest.mark.asyncio
async def test_frontend_close_fails_requests_on_old_peer():
    sent = asyncio.Queue()

    async def send(payload):
        sent.put_nowait(json.loads(payload))

    requests = FrontendRequests(send)
    task = asyncio.create_task(requests.request("pi.settings.update", {}, timeout=1))
    await sent.get()
    requests.close()
    with pytest.raises(RuntimeConnectionClosed):
        await task
    with pytest.raises(RuntimeConnectionClosed):
        await requests.request("pi.snapshot", {}, timeout=1)
    assert requests._pending == {}


@pytest.mark.asyncio
async def test_frontend_oversized_request_is_rejected_before_transmission():
    async def send(_payload):
        pytest.fail("oversized request must not reach the writer")

    requests = FrontendRequests(send)
    with pytest.raises(RuntimePayloadTooLarge):
        await requests.request("pi.settings.update", {"value": "x" * 65536}, timeout=1)
    requests.close()


async def test_frontend_close_aborts_a_peer_that_never_drains(monkeypatch):
    monkeypatch.setattr(
        "theater.daemon.harness_runtime.frontend.FRONTEND_WRITER_CLOSE_TIMEOUT_SECONDS", 0.01
    )

    class BlockedWriter:
        def __init__(self):
            self.transport = self
            self.aborted = False

        def close(self):
            pass

        async def wait_closed(self):
            await asyncio.Event().wait()

        def abort(self):
            self.aborted = True

    writer = BlockedWriter()
    connection = UnixFrontendConnection(writer)
    await asyncio.wait_for(connection.aclose(), timeout=0.5)
    assert connection.closed
    assert writer.aborted


def _short_runtime_dir(monkeypatch, tmp_path) -> Path:
    directory = Path("/tmp") / tmp_path.name
    directory.mkdir(exist_ok=True)
    monkeypatch.setattr("theater.daemon.runtime.wiring.backend_artifacts_dir", lambda _: directory)
    monkeypatch.setattr(
        "theater.daemon.harness_runtime.frontend.backend_artifacts_dir", lambda _: directory
    )
    return directory


async def _connect(endpoint: str, token: str):
    path = endpoint.removeprefix("unix://")
    reader, writer = await asyncio.open_unix_connection(path)
    hello = {"type": "hello", "protocol": "theater-frontend-v1", "token": token}
    writer.write(json.dumps(hello).encode())
    writer.write(b"\n")
    await writer.drain()
    return reader, writer


async def _event(writer, payload: dict) -> None:
    writer.write(json.dumps({"type": "event", "event": payload}).encode())
    writer.write(b"\n")
    await writer.drain()


async def _close(writer) -> None:
    writer.close()
    await writer.wait_closed()


async def _wait_for_count(items: list[object], count: int) -> None:
    for _ in range(20):
        if len(items) >= count:
            return
        await asyncio.sleep(0)
    assert len(items) >= count


async def test_listener_authenticates_and_forwards_bounded_notifications(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    _short_runtime_dir(monkeypatch, tmp_path)
    host = FrontendRuntimeHost()
    endpoint = frontend_endpoint("open-1")
    connected: list[RuntimeFrontendConnection] = []
    disconnected: list[RuntimeFrontendConnection] = []

    async def on_connect(connection: RuntimeFrontendConnection) -> None:
        connected.append(connection)

    async def on_disconnect(connection: RuntimeFrontendConnection) -> None:
        disconnected.append(connection)

    await host.start(
        participant_id="open-1",
        generation=1,
        endpoint=endpoint,
        token="secret",
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )
    _reader, rejected = await _connect(endpoint, "wrong")
    await asyncio.sleep(0)
    assert connected == []
    await _close(rejected)

    _reader, writer = await _connect(endpoint, "secret")
    await _wait_for_count(connected, 1)
    assert len(connected) == 1
    await _event(writer, {"id": "event-1", "type": "message.updated"})
    notification = await anext(connected[0].notifications())
    assert notification.method == "event"
    assert notification.params["event"]["id"] == "event-1"
    await _close(writer)
    await _wait_for_count(disconnected, 1)
    assert disconnected == connected
    await host.aclose()


async def test_listener_replacement_disconnects_only_the_replaced_connection(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    _short_runtime_dir(monkeypatch, tmp_path)
    host = FrontendRuntimeHost()
    endpoint = frontend_endpoint("open-1")
    connected: list[RuntimeFrontendConnection] = []
    disconnected: list[RuntimeFrontendConnection] = []

    async def on_connect(connection: RuntimeFrontendConnection) -> None:
        connected.append(connection)

    async def on_disconnect(connection: RuntimeFrontendConnection) -> None:
        disconnected.append(connection)

    await host.start(
        participant_id="open-1",
        generation=1,
        endpoint=endpoint,
        token="secret",
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )
    _reader, first = await _connect(endpoint, "secret")
    await _wait_for_count(connected, 1)
    _reader, second = await _connect(endpoint, "secret")
    await _wait_for_count(connected, 2)
    assert len(connected) == 2
    assert disconnected == [connected[0]]
    assert not connected[1].closed
    await _close(first)
    await _close(second)
    await _wait_for_count(disconnected, 2)
    assert disconnected == connected
    await host.aclose()


async def test_listener_serializes_replacing_connection_activation(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    _short_runtime_dir(monkeypatch, tmp_path)
    host = FrontendRuntimeHost()
    endpoint = frontend_endpoint("open-1")
    connected: list[RuntimeFrontendConnection] = []
    disconnected: list[RuntimeFrontendConnection] = []
    first_ready = asyncio.Event()
    release_first = asyncio.Event()

    async def on_connect(connection: RuntimeFrontendConnection) -> None:
        connected.append(connection)
        if len(connected) == 1:
            first_ready.set()
            await release_first.wait()

    async def on_disconnect(connection: RuntimeFrontendConnection) -> None:
        disconnected.append(connection)

    await host.start(
        participant_id="open-1",
        generation=1,
        endpoint=endpoint,
        token="secret",
        on_connect=on_connect,
        on_disconnect=on_disconnect,
    )
    _reader, first = await _connect(endpoint, "secret")
    await first_ready.wait()
    _reader, second = await _connect(endpoint, "secret")
    await asyncio.sleep(0)
    assert len(connected) == 1

    release_first.set()
    await _wait_for_count(connected, 2)
    assert disconnected == [connected[0]]
    await _close(first)
    await _close(second)
    await host.aclose()


async def test_listener_refuses_an_endpoint_outside_participant_runtime(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    host = FrontendRuntimeHost()

    async def callback(connection: RuntimeFrontendConnection) -> None:
        del connection

    with pytest.raises(FrontendProtocolError, match="outside"):
        await host.start(
            participant_id="open-1",
            generation=1,
            endpoint=f"unix://{tmp_path / 'outside.sock'}",
            token="secret",
            on_connect=callback,
            on_disconnect=callback,
        )


async def test_closed_connection_drains_a_full_queue_then_terminates() -> None:
    class Writer:
        def close(self) -> None:
            pass

        async def wait_closed(self) -> None:
            pass

    connection = UnixFrontendConnection(Writer())  # type: ignore[arg-type]
    for index in range(FRONTEND_QUEUE_MAX):
        connection.publish(RuntimeNotification(method="snapshot", params={"index": index}))

    await connection.aclose()

    async def drain() -> list[RuntimeNotification]:
        return [notification async for notification in connection.notifications()]

    drained = await asyncio.wait_for(drain(), timeout=0.5)
    assert len(drained) == FRONTEND_QUEUE_MAX


async def test_initial_attachment_can_request_a_snapshot(monkeypatch, tmp_path) -> None:
    _short_runtime_dir(monkeypatch, tmp_path)
    host = FrontendRuntimeHost()
    endpoint = frontend_endpoint("pi-1")
    ready = asyncio.Event()
    received = []

    async def activate(connection):
        received.append(await connection.request("pi.snapshot", {}, timeout=1))
        ready.set()

    async def disconnect(connection):
        pass

    await host.start(
        participant_id="pi-1",
        generation=1,
        endpoint=endpoint,
        token="secret",
        on_connect=activate,
        on_disconnect=disconnect,
    )
    try:
        reader, writer = await _connect(endpoint, "secret")
        request = json.loads(await asyncio.wait_for(reader.readline(), timeout=1))
        assert request["method"] == "pi.snapshot"
        writer.write(
            json.dumps(
                {
                    "type": "response",
                    "id": request["id"],
                    "result": {"session": "a"},
                }
            ).encode()
            + b"\n"
        )
        await writer.drain()
        await asyncio.wait_for(ready.wait(), timeout=1)
        assert received == [{"session": "a"}]
        await _close(writer)
    finally:
        await host.aclose()


async def test_listener_shutdown_cancels_incomplete_activation(monkeypatch, tmp_path) -> None:
    _short_runtime_dir(monkeypatch, tmp_path)
    host = FrontendRuntimeHost()
    endpoint = frontend_endpoint("pi-1")
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def activate(connection):
        started.set()
        try:
            await connection.request("pi.snapshot", {}, timeout=10)
        finally:
            stopped.set()

    async def disconnect(connection):
        pass

    await host.start(
        participant_id="pi-1",
        generation=1,
        endpoint=endpoint,
        token="secret",
        on_connect=activate,
        on_disconnect=disconnect,
    )
    reader, writer = await _connect(endpoint, "secret")
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(host.aclose(), timeout=1)
    assert stopped.is_set()
    # The only written request was never replayed during shutdown.
    frames = (await reader.read()).splitlines()
    assert len(frames) == 1
    assert json.loads(frames[0])["method"] == "pi.snapshot"
    await _close(writer)


class _Observer(TranscriptObserver):
    has_transcript = False

    def find_transcript(self, *, cwd, session_id=None, after=None):
        del cwd, session_id, after

    def session_id(self, transcript):
        del transcript

    def parse(self, line, index, *, clip_text=True):
        del line, index, clip_text
        return []

    def is_idle_screen(self, capture):
        del capture
        return False

    def validate_transcript_receipt(self, *, payload, cwd, expected_session_id):
        del payload, cwd, expected_session_id


class _BoundFrontendRuntime(OpenCodeFrontendRuntime):
    def __init__(self, context, native_session_id: str) -> None:
        super().__init__(context)
        self._native_session_id = native_session_id

    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        del mode, native_session_id
        return RuntimeBinding(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.ATTACHED,
            endpoint=self.context.endpoint,
            native_session_id=self._native_session_id,
        )

    async def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            native_session_id=self._native_session_id,
            capabilities=RuntimeCapabilities(available={RuntimeCapability.SEND}),
            health=ConnectionHealth.CONNECTED,
            execution_state=RuntimeExecutionState.IDLE,
        )


class _FrontendHarness(Harness):
    name = "frontend-test"
    binary = sys.executable
    icon = "F"
    launch_parameter_support = LaunchParameterSupport(model=True)

    def __init__(self, *, fail_install: bool = False, native_session_id: str | None = None) -> None:
        self.observer = _Observer()
        self.fail_install = fail_install
        factory = opencode_frontend_runtime_factory
        if native_session_id is not None:

            def bound_factory(context):
                return _BoundFrontendRuntime(context, native_session_id)

            factory = bound_factory
        self.runtime = RuntimeManifest(
            probe=lambda context: RuntimeCompatibility(
                supported=True,
                policy="frontend-test-1",
                native_version="1.18.29",
            ),
            plan=None,
            factory=factory,
            channel=LiveChannelDeclaration(
                channel=ChannelDeclaration(id="frontend-live", kind=ChannelKind.LIVE),
                drives_job_completion=False,
            ),
            host=RuntimeHost.FRONTEND,
            frontend_installer=self._install,
            legacy_fallback=frozenset(
                {
                    RuntimeCapability.SEND,
                    RuntimeCapability.QUEUE_FOLLOWUP,
                    RuntimeCapability.INTERRUPT,
                }
            ),
        )

    def _install(self, context) -> RuntimeFrontendOverlay:
        if self.fail_install:
            raise RuntimeError("test installer refused")
        path = (
            paths.participant_observation_dir(context.participant_id, self.name) / "frontend.json"
        )
        return RuntimeFrontendOverlay(env={"FRONTEND_TEST": "1"}, files={path: "{}"})

    def plan_launch(
        self,
        *,
        participant_id: str,
        prompt: str,
        config_path: Path,
        approval: str,
        model: str | None = None,
        mcp_servers=(),
    ) -> LaunchPlan:
        del config_path, approval, model, mcp_servers
        return LaunchPlan(
            argv=[self.binary, "-c", "pass"],
            receipt_token_path=paths.participant_observation_dir(participant_id, self.name)
            / "receipt-token",
        )


async def _daemon(harness: _FrontendHarness) -> Daemon:
    daemon = Daemon(harnesses={})
    HARNESSES[harness.name] = harness
    await daemon.start()
    return daemon


async def _spawn(daemon: Daemon, request: SpawnRequest):
    accepted = await ParticipantLaunchService(daemon).spawn(
        client_id="frontend-spawning-test",
        idempotency_key=f"spawn-{len(daemon.registry.list())}",
        params={
            "harness": request.harness,
            "prompt": request.prompt,
            "cwd": request.cwd,
            "approval": request.approval,
        },
    )
    operation, timed_out = await daemon.operation_service.wait(str(accepted["operation_id"]))
    assert not timed_out and operation.state == "succeeded"
    return daemon.registry.get(str(accepted["participant_id"]))


def _request() -> SpawnRequest:
    return SpawnRequest(
        harness="frontend-test",
        prompt="stock prompt",
        cwd="/tmp",
        approval="manual",
    )


async def test_frontend_spawn_keeps_the_stock_plan_and_passive_binding(terminal_provider) -> None:
    terminal_provider.terminals.clear()
    daemon = await _daemon(_FrontendHarness())
    try:
        participant = await _spawn(daemon, _request())
        binding = daemon.store.get_runtime_binding(participant.id)
        assert binding is not None
        assert binding.native_version == "1.18.29"
        assert binding.compatibility_policy == "frontend-test-1"
        assert daemon.runtime_manager.get(participant.id) is None
        assert daemon.controls.route_for(participant.id, RuntimeCapability.SEND).is_provider
        assert terminal_provider.creations[0]["command"] == [sys.executable, "-c", "pass"]
        assert terminal_provider.creations[0]["env"]["FRONTEND_TEST"] == "1"
    finally:
        await daemon.aclose()


async def test_frontend_connection_keeps_an_active_stock_launch_active(terminal_provider) -> None:
    terminal_provider.terminals.clear()
    daemon = await _daemon(_FrontendHarness())
    writer = None
    try:
        participant = await _spawn(daemon, _request())
        binding = daemon.store.get_runtime_binding(participant.id)
        assert binding is not None and binding.endpoint is not None
        credential = daemon.store.get_channel_credential(
            participant.id, ChannelKind.LIVE, "frontend-live"
        )
        assert credential is not None
        _reader, writer = await asyncio.open_unix_connection(
            binding.endpoint.removeprefix("unix://")
        )
        writer.write(
            (
                json.dumps(
                    {"type": "hello", "protocol": "theater-frontend-v1", "token": credential.token}
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        for _ in range(20):
            if daemon.runtime_manager.get(participant.id) is not None:
                break
            await asyncio.sleep(0)
        assert daemon.runtime_manager.get(participant.id) is not None
        binding = daemon.store.get_runtime_binding(participant.id)
        assert binding is not None
        assert binding.lifecycle is RuntimeLifecyclePhase.ACTIVE
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await daemon.aclose()


async def test_frontend_connection_persists_and_caches_its_exact_session(
    terminal_provider,
) -> None:
    terminal_provider.terminals.clear()
    daemon = await _daemon(_FrontendHarness(native_session_id="frontend-session-a"))
    writer = None
    try:
        participant = await _spawn(daemon, _request())
        binding = daemon.store.get_runtime_binding(participant.id)
        assert binding is not None and binding.endpoint is not None
        credential = daemon.store.get_channel_credential(
            participant.id, ChannelKind.LIVE, "frontend-live"
        )
        assert credential is not None
        _reader, writer = await asyncio.open_unix_connection(
            binding.endpoint.removeprefix("unix://")
        )
        writer.write(
            (
                json.dumps(
                    {"type": "hello", "protocol": "theater-frontend-v1", "token": credential.token}
                )
                + "\n"
            ).encode()
        )
        await writer.drain()
        for _ in range(20):
            binding = daemon.store.get_runtime_binding(participant.id)
            if binding is not None and binding.native_session_id == "frontend-session-a":
                break
            await asyncio.sleep(0)

        assert binding is not None and binding.native_session_id == "frontend-session-a"
        current = daemon.registry.get(participant.id)
        assert (current.session_id, current.session_correlation) == (
            "frontend-session-a",
            "exact",
        )
        assert daemon.runtime_manager.cached_native_route(
            participant.id,
            backend_generation=binding.backend_generation,
            native_session_id="frontend-session-a",
        ) == {
            "backend_generation": binding.backend_generation,
            "native_session_id": "frontend-session-a",
            "health": "connected",
        }
        capabilities = daemon.runtime_manager.cached_native_capabilities(
            participant.id,
            backend_generation=binding.backend_generation,
            native_session_id="frontend-session-a",
        )
        assert capabilities is not None
        assert capabilities.supports(RuntimeCapability.SEND)
    finally:
        if writer is not None:
            writer.close()
            await writer.wait_closed()
        await daemon.aclose()


async def test_frontend_recovery_keeps_a_proven_unsent_legacy_queue(terminal_provider) -> None:
    terminal_provider.terminals.clear()
    harness = _FrontendHarness()
    first = await _daemon(harness)
    second = None
    try:
        participant = await _spawn(first, _request())
        first.registry.set_status(participant.id, Status.WORKING)
        queued = await first.controls.queue_followup(
            participant.id,
            caller_id="cli",
            prompt="after restart",
        )
        await first.aclose()
        first = None

        second = Daemon(harnesses={})
        HARNESSES[harness.name] = harness
        await second.start()

        (operation,) = second.store.queued_control_operations(participant.id)
        assert operation.job_handle == queued.handle
        assert operation.transport is ControlTransport.PROVIDER_TERMINAL
        assert operation.delivery_phase is ControlDeliveryPhase.QUEUED
        assert second.store.get_job(queued.handle).state == "running"
    finally:
        if first is not None:
            await first.aclose()
        if second is not None:
            await second.aclose()


async def test_frontend_install_failure_keeps_the_ordinary_launch(terminal_provider) -> None:
    terminal_provider.terminals.clear()
    daemon = await _daemon(_FrontendHarness(fail_install=True))
    try:
        participant = await _spawn(daemon, _request())
        assert daemon.store.get_runtime_binding(participant.id) is None
        assert terminal_provider.creations[0]["command"] == [sys.executable, "-c", "pass"]
        assert "FRONTEND_TEST" not in terminal_provider.creations[0]["env"]
    finally:
        await daemon.aclose()


@pytest.mark.parametrize("invalid_result", [False, True])
async def test_optional_probe_failure_keeps_the_ordinary_launch(terminal_provider, invalid_result):
    terminal_provider.terminals.clear()
    harness = _FrontendHarness()

    def broken_probe(_context):
        if invalid_result:
            return {}
        raise RuntimeError("probe unavailable")

    harness.runtime = replace(harness.runtime, probe=broken_probe)
    daemon = await _daemon(harness)
    try:
        participant = await _spawn(daemon, _request())
        assert daemon.store.get_runtime_binding(participant.id) is None
        assert terminal_provider.creations[0]["command"] == [sys.executable, "-c", "pass"]
        assert "FRONTEND_TEST" not in terminal_provider.creations[0]["env"]
    finally:
        await daemon.aclose()


async def test_listener_start_failure_falls_back_before_the_pane_launch(
    terminal_provider, monkeypatch
) -> None:
    terminal_provider.terminals.clear()
    daemon = await _daemon(_FrontendHarness())

    async def refuse(**kwargs) -> None:
        del kwargs
        raise OSError("listener unavailable")

    monkeypatch.setattr(daemon.frontend_runtime_host, "start", refuse)
    try:
        participant = await _spawn(daemon, _request())
        assert daemon.store.get_runtime_binding(participant.id) is None
        assert len(terminal_provider.creations) == 1
        assert "FRONTEND_TEST" not in terminal_provider.creations[0]["env"]
    finally:
        await daemon.aclose()
