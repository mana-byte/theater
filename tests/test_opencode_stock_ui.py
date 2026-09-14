"""Opt-in conformance gate for the installed stock OpenCode TUI."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.harness_runtime.frontend import FrontendRuntimeHost
from theater.daemon.runtime.wiring import frontend_endpoint
from theater.daemon.spawning.planning import (
    install_frontend_plan,
    validate_receipt_plan,
    write_plan_files,
)
from theater.harness.builtin.plugins.opencode.launch import plan_launch
from theater.harness.builtin.plugins.opencode.live import OpenCodeTuiLiveSource
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST
from theater.harness.builtin.plugins.opencode.runtime import OpenCodeFrontendRuntime
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    DeliveryResult,
    NativeTurnTerminal,
    RuntimeCapability,
    RuntimeContext,
    RuntimeFrontendConnection,
    RuntimeIO,
    RuntimeNotification,
)
from theater.harness.contracts.source import Batch
from theater.models import Participant, Status

pytestmark = pytest.mark.tmux


class _StockIO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float):
        del timeout
        raise AssertionError(f"the stock gate never opens a backend connection: {endpoint}")


class _RecordingConnection(RuntimeFrontendConnection):
    """Records notifications while the runtime stays the sole consumer."""

    def __init__(self, inner: RuntimeFrontendConnection, sink: list[RuntimeNotification]) -> None:
        self._inner = inner
        self._sink = sink

    @property
    def closed(self) -> bool:
        return self._inner.closed

    async def notifications(self) -> AsyncIterator[RuntimeNotification]:
        async for item in self._inner.notifications():
            self._sink.append(item)
            yield item

    async def request(self, method: str, params: Mapping[str, object], *, timeout: float):
        return await self._inner.request(method, params, timeout=timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()


def _assistant_parent_ids(probe: _Probe) -> list[object]:
    """Every parentID the forwarded assistant message.updated events named."""
    parents: list[object] = []
    for item in probe.notifications:
        event = item.params.get("event")
        if not isinstance(event, Mapping) or event.get("type") != "message.updated":
            continue
        properties = event.get("properties")
        info = properties.get("info") if isinstance(properties, Mapping) else None
        if isinstance(info, Mapping) and info.get("role") == "assistant":
            parents.append(info.get("parentID"))
    return parents


@dataclass(slots=True)
class _Probe:
    """Daemon-shaped wiring: one OpenCode runtime per plugin connection."""

    notifications: list[RuntimeNotification] = field(default_factory=list)
    connections: list[RuntimeFrontendConnection] = field(default_factory=list)
    disconnected: list[RuntimeFrontendConnection] = field(default_factory=list)
    trusted_session_id: str | None = None
    runtimes: list[OpenCodeFrontendRuntime] = field(default_factory=list)

    @property
    def live(self) -> OpenCodeTuiLiveSource:
        assert self.runtimes, "no plugin connection yet"
        return self.runtimes[-1].live_source()

    async def on_connect(self, connection: RuntimeFrontendConnection) -> None:
        self.connections.append(connection)
        runtime = OpenCodeFrontendRuntime(
            RuntimeContext(
                participant_id="open-1",
                cwd="/tmp",
                io=_StockIO(),
                backend_generation=1,
                endpoint="unix:///tmp/oc-stock.sock",
                frontend=_RecordingConnection(connection, self.notifications),
                trusted_session_id_provider=lambda: self.trusted_session_id,
            )
        )
        self.runtimes.append(runtime)
        runtime.live_source()

    async def on_disconnect(self, connection: RuntimeFrontendConnection) -> None:
        self.disconnected.append(connection)


@dataclass(frozen=True, slots=True)
class _StockLaunch:
    participant: Participant
    endpoint: str
    plan: LaunchPlan
    token: str
    session_id: str


class _OpenAICompatibleProvider:
    def __init__(self) -> None:
        self.request_received = asyncio.Event()
        self.response_completed = asyncio.Event()
        self._release = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self.requests: list[tuple[str, dict[str, object]]] = []
        self.errors: list[str] = []

    @property
    def base_url(self) -> str:
        assert self._server is not None
        socket = next(iter(self._server.sockets or ()), None)
        assert socket is not None
        host, port = socket.getsockname()[:2]
        return f"http://{host}:{port}/v1"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, host="127.0.0.1", port=0)

    def release(self) -> None:
        self._release.set()

    async def aclose(self) -> None:
        self._release.set()
        server = self._server
        if server is not None:
            server.close()
        for writer in tuple(self._writers):
            writer.close()
        for task in tuple(self._handlers):
            if not task.done():
                task.cancel()
        if self._handlers:
            await asyncio.gather(*self._handlers, return_exceptions=True)
        if server is not None:
            await server.wait_closed()

    def detail(self) -> str:
        return f"requests={self.requests!r}, errors={self.errors!r}"

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        self._writers.add(writer)
        try:
            request_line = (await reader.readline()).decode("latin-1").rstrip("\r\n")
            method, target, _version = request_line.split(" ", 2)
            headers = await _http_headers(reader)
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length) if length else b""
            payload = json.loads(body) if body else {}
            if not isinstance(payload, dict):
                self.errors.append("OpenAI-compatible request body was not an object")
                return
            self.requests.append((target, payload))
            if method == "GET" and target == "/v1/models":
                await _http_json(writer, {"object": "list", "data": [{"id": "test-model"}]})
                return
            if method != "POST" or target != "/v1/chat/completions":
                await _http_json(
                    writer, {"error": {"message": "unsupported test endpoint"}}, status=404
                )
                return
            self.request_received.set()
            writer.write(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/event-stream\r\n"
                b"Cache-Control: no-cache\r\n"
                b"Connection: close\r\n\r\n"
            )
            await writer.drain()
            await self._release.wait()
            for chunk in _chat_completion_chunks():
                writer.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                await writer.drain()
            writer.write(b"data: [DONE]\n\n")
            await writer.drain()
            self.response_completed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            self._writers.discard(writer)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._handlers.discard(task)


async def _http_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while line := await reader.readline():
        if line in {b"\r\n", b"\n"}:
            return headers
        key, value = line.decode("latin-1").split(":", 1)
        headers[key.lower()] = value.strip()
    raise ConnectionError("OpenAI-compatible request ended before headers")


async def _http_json(
    writer: asyncio.StreamWriter, payload: Mapping[str, object], *, status: int = 200
) -> None:
    body = json.dumps(payload).encode()
    header = (
        f"HTTP/1.1 {status} {'OK' if status == 200 else 'Not Found'}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    writer.write(header + body)
    await writer.drain()


def _chat_completion_chunks() -> tuple[dict[str, object], ...]:
    common = {
        "id": "chatcmpl-stock-proof",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test-model",
    }
    return (
        {
            **common,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "stock streamed answer"},
                    "finish_reason": None,
                }
            ],
        },
        {
            **common,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
    )


def _enabled() -> bool:
    return os.environ.get("THEATER_OPENCODE_STOCK_CONFORMANCE") == "1"


def _tmux(
    socket: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", str(socket), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )


async def _tmux_run(
    socket: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return await asyncio.to_thread(_tmux, socket, *args, env=env)


def _capture(socket: Path, session: str) -> str:
    try:
        return _tmux(socket, "capture-pane", "-p", "-t", session, "-S", "-120").stdout
    except subprocess.SubprocessError:
        return "<pane unavailable>"


def _stock_version(binary: str) -> str:
    return subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=15, check=True
    ).stdout


async def _until(predicate, *, timeout: float, detail) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(detail())


def _prepare_frontend_plan(plan: LaunchPlan, participant: Participant) -> tuple[LaunchPlan, str]:
    credential = next(
        (item for item in plan.channel_credentials if item.kind is ChannelKind.LIVE),
        None,
    )
    if credential is not None:
        write_plan_files(plan)
        return plan, credential.token
    token = validate_receipt_plan(plan, participant)
    assert token is not None
    prepared = replace(plan, receipt_token=token)
    write_plan_files(prepared)
    return prepared, token


def _configure_isolation(monkeypatch, root: Path, marker: Path, provider_base_url: str) -> Path:
    cwd = root / "cwd"
    config = root / "xdg-config" / "opencode"
    for directory in (
        cwd,
        config,
        root / "home",
        root / "tmp",
        root / "xdg-data",
        root / "xdg-cache",
        root / "xdg-state",
    ):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    monkeypatch.setenv("THEATER_HOME", str(root / "theater"))
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "xdg-data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "xdg-cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "xdg-state"))
    monkeypatch.setenv("TMPDIR", str(root / "tmp"))
    monkeypatch.setenv("THEATER_OPENCODE_PLUGIN_MARKER", str(marker))
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    paths.ensure_home()
    _write_user_config(config, provider_base_url)
    return cwd


def _write_user_config(config: Path, provider_base_url: str) -> None:
    user_plugin = config / "user-plugin.mjs"
    user_plugin.write_text(
        'import { appendFile } from "node:fs/promises"\n'
        "const marker = process.env.THEATER_OPENCODE_PLUGIN_MARKER\n"
        "const tui = async (api) => {\n"
        "  await appendFile(marker, `loaded:${api.tuiConfig.scroll_speed}\\n`)\n"
        '  api.lifecycle.onDispose(() => appendFile(marker, "disposed\\n"))\n'
        "}\n"
        'export default { id: "test.user-plugin", tui }\n'
    )
    (config / "tui.json").write_text(
        json.dumps({"plugin": [user_plugin.as_uri()], "scroll_speed": 7})
    )
    (config / "opencode.json").write_text(
        json.dumps(
            {
                "enabled_providers": ["test"],
                "provider": {
                    "test": {
                        "name": "Test",
                        "id": "test",
                        "env": [],
                        "npm": "@ai-sdk/openai-compatible",
                        "models": {
                            "test-model": {
                                "id": "test-model",
                                "name": "Test Model",
                                "attachment": False,
                                "reasoning": False,
                                "temperature": False,
                                "tool_call": True,
                                "limit": {"context": 100000, "output": 10000},
                                "cost": {"input": 0, "output": 0},
                            }
                        },
                        "options": {"apiKey": "test-key", "baseURL": provider_base_url},
                    }
                },
            }
        )
    )


def _frontend_plan(
    participant: Participant,
    runtime,
    endpoint: str,
    *,
    prompt: str,
    resume: str | None = None,
) -> tuple[LaunchPlan, str]:
    plan = plan_launch(
        LaunchContext(
            participant_id=participant.id,
            prompt=prompt,
            config_path=paths.mcp_config_path(participant.id),
            approval="yolo",
            model="test/test-model",
            resume=resume,
        )
    )
    return _prepare_frontend_plan(
        install_frontend_plan(plan, participant, runtime, endpoint),
        participant,
    )


def _environment(plan: LaunchPlan, participant: Participant) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(plan.env)
    environment.update(TERM="xterm-256color", THEATER_ID=participant.id)
    environment.pop("TMUX", None)
    environment.pop("TMUX_PANE", None)
    return environment


async def _start_listener(
    host: FrontendRuntimeHost,
    participant: Participant,
    endpoint: str,
    token: str,
    probe: _Probe,
) -> None:
    await host.start(
        participant_id=participant.id,
        generation=1,
        endpoint=endpoint,
        token=token,
        on_connect=probe.on_connect,
        on_disconnect=probe.on_disconnect,
    )


async def _launch_pane(
    socket: Path,
    session: str,
    cwd: Path,
    plan: LaunchPlan,
    environment: dict[str, str],
) -> None:
    await _tmux_run(
        socket,
        "new-session",
        "-d",
        "-s",
        session,
        "-c",
        str(cwd),
        "-x",
        "120",
        "-y",
        "40",
        *plan.argv,
        env=environment,
    )


async def _launch_initial(
    host: FrontendRuntimeHost,
    socket: Path,
    cwd: Path,
    marker: Path,
    runtime,
    probe: _Probe,
) -> _StockLaunch:
    participant = Participant(id="open-1", harness="opencode")
    endpoint = frontend_endpoint(participant.id)
    plan, token = _frontend_plan(participant, runtime, endpoint, prompt="stock status prompt")
    await _start_listener(host, participant, endpoint, token, probe)
    environment = _environment(plan, participant)
    await _launch_pane(socket, "oc-stock", cwd, plan, environment)
    await _until(
        lambda: any(_visible_session_snapshot(item) for item in probe.notifications),
        timeout=30,
        detail=lambda: _capture(socket, "oc-stock"),
    )
    await _until(
        lambda: marker.exists() and "loaded:7" in marker.read_text(),
        timeout=15,
        detail=lambda: _capture(socket, "oc-stock"),
    )
    snapshot = next(item for item in probe.notifications if _visible_session_snapshot(item))
    session_id = snapshot.params["session_id"]
    assert isinstance(session_id, str) and session_id
    probe.trusted_session_id = session_id
    assert "--model" in plan.argv and "--auto" in plan.argv
    return _StockLaunch(participant, endpoint, plan, token, session_id)


async def _restart_listener(
    host: FrontendRuntimeHost,
    launch: _StockLaunch,
    probe: _Probe,
    socket: Path,
    expected_status: str,
) -> None:
    active = probe.connections[-1]
    await active.aclose()
    await _until(
        lambda: active in probe.disconnected,
        timeout=10,
        detail=lambda: _capture(socket, "oc-stock"),
    )
    await host.close(launch.participant.id)
    await _start_listener(host, launch.participant, launch.endpoint, launch.token, probe)
    before_reconnect = len(probe.notifications)
    await _until(
        lambda: (
            len(probe.connections) >= 2
            and any(
                _is_snapshot(item, launch.session_id, expected_status)
                for item in probe.notifications[before_reconnect:]
            )
        ),
        timeout=15,
        detail=lambda: _capture(socket, "oc-stock"),
    )


async def _launch_fork(
    host: FrontendRuntimeHost,
    socket: Path,
    cwd: Path,
    runtime,
    parent: _StockLaunch,
    probe: _Probe,
) -> None:
    participant = Participant(id="open-2", harness="opencode")
    endpoint = frontend_endpoint(participant.id)
    plan, token = _frontend_plan(
        participant, runtime, endpoint, prompt="", resume=parent.session_id
    )
    await _start_listener(host, participant, endpoint, token, probe)
    assert plan.argv[-3:] == ["-s", parent.session_id, "--fork"]
    assert plan.env["OPENCODE_DB"] == parent.plan.env["OPENCODE_DB"]
    await _launch_pane(socket, "oc-stock-fork", cwd, plan, _environment(plan, participant))
    await _until(
        lambda: any(
            _visible_session_snapshot(item) and item.params.get("session_id") != parent.session_id
            for item in probe.notifications
        ),
        timeout=30,
        detail=lambda: _capture(socket, "oc-stock-fork"),
    )


async def _cleanup(host: FrontendRuntimeHost, socket: Path, probes: tuple[_Probe, ...]) -> None:
    with contextlib.suppress(subprocess.SubprocessError):
        await _tmux_run(socket, "kill-server")
    for connection in (connection for probe in probes for connection in probe.connections):
        if not connection.closed:
            await connection.aclose()
    await host.aclose()
    for runtime in (runtime for probe in probes for runtime in probe.runtimes):
        await runtime.aclose()


def _visible_session_snapshot(notification: RuntimeNotification) -> bool:
    session_id = notification.params.get("session_id")
    return (
        notification.method == "snapshot"
        and isinstance(session_id, str)
        and bool(session_id)
        and notification.params.get("route_session_id") == session_id
    )


def _is_status_event(notification: RuntimeNotification, session_id: str, status: str) -> bool:
    event = notification.params.get("event")
    if notification.method != "event" or not isinstance(event, Mapping):
        return False
    properties = event.get("properties")
    if not isinstance(properties, Mapping):
        return False
    event_status = properties.get("status")
    return (
        notification.params.get("session_id") == session_id
        and notification.params.get("route_session_id") == session_id
        and isinstance(notification.params.get("session_epoch"), int)
        and event.get("type") == "session.status"
        and properties.get("sessionID") == session_id
        and isinstance(event_status, Mapping)
        and event_status.get("type") == status
    )


def _is_snapshot(notification: RuntimeNotification, session_id: str, status: str) -> bool:
    snapshot_status = notification.params.get("status")
    return (
        notification.method == "snapshot"
        and notification.params.get("session_id") == session_id
        and notification.params.get("route_session_id") == session_id
        and isinstance(snapshot_status, Mapping)
        and snapshot_status.get("type") == status
    )


@pytest.mark.skipif(not _enabled(), reason="set THEATER_OPENCODE_STOCK_CONFORMANCE=1")
async def test_stock_tui_loads_passive_extension_and_reconnects(monkeypatch) -> None:
    binary = shutil.which("opencode")
    if binary is None or shutil.which("tmux") is None:
        pytest.skip("opencode and tmux are required")
    assert binary is not None
    version = await asyncio.to_thread(_stock_version, binary)
    if "1.18.29" not in version:
        pytest.skip(f"expected stock OpenCode 1.18.29, found {version.strip()!r}")

    root = Path(tempfile.mkdtemp(prefix="oc-stock-", dir="/tmp")).resolve()
    socket = root / "tmux.sock"
    marker = root / "user-plugin.log"
    host = FrontendRuntimeHost()
    initial_probe = _Probe()
    fork_probe = _Probe()
    provider = _OpenAICompatibleProvider()
    try:
        await provider.start()
        cwd = _configure_isolation(monkeypatch, root, marker, provider.base_url)
        runtime = MANIFEST.runtime
        assert runtime is not None
        initial = await _launch_initial(host, socket, cwd, marker, runtime, initial_probe)
        await _until(
            provider.request_received.is_set,
            timeout=15,
            detail=lambda: f"{_capture(socket, 'oc-stock')}\n{provider.detail()}",
        )
        target, request = provider.requests[-1]
        assert target == "/v1/chat/completions"
        assert request.get("model") == "test-model"
        assert request.get("stream") is True
        await _until(
            lambda: any(
                _is_status_event(item, initial.session_id, "busy")
                for item in initial_probe.notifications
            ),
            timeout=15,
            detail=lambda: f"{_capture(socket, 'oc-stock')}\n{provider.detail()}",
        )
        assert (await initial_probe.live.read()).status is Status.WORKING
        provider.release()
        await _until(
            provider.response_completed.is_set,
            timeout=15,
            detail=lambda: f"{_capture(socket, 'oc-stock')}\n{provider.detail()}",
        )
        await _until(
            lambda: any(
                _is_status_event(item, initial.session_id, "idle")
                for item in initial_probe.notifications
            ),
            timeout=15,
            detail=lambda: f"{_capture(socket, 'oc-stock')}\n{provider.detail()}",
        )
        await _until(
            lambda: "stock streamed answer" in _capture(socket, "oc-stock"),
            timeout=15,
            detail=lambda: _capture(socket, "oc-stock"),
        )
        assert not provider.errors, provider.detail()
        await _restart_listener(host, initial, initial_probe, socket, "idle")
        assert (await initial_probe.live.read()).status is Status.IDLE
        await _tmux_run(socket, "kill-session", "-t", "oc-stock")
        await _until(
            lambda: marker.exists() and "disposed" in marker.read_text(),
            timeout=15,
            detail=lambda: _capture(socket, "oc-stock"),
        )
        await _launch_fork(host, socket, cwd, runtime, initial, fork_probe)
        await _tmux_run(socket, "kill-session", "-t", "oc-stock-fork")
    finally:
        await provider.aclose()
        await _cleanup(host, socket, (initial_probe, fork_probe))
        shutil.rmtree(root, ignore_errors=True)


async def _wait_for_batch(
    probe: _Probe,
    socket: Path,
    *,
    timeout: float,
    want: Callable[[Batch], bool],
) -> Batch:
    deadline = time.monotonic() + timeout
    batch: Batch | None = None
    while time.monotonic() < deadline:
        batch = await probe.live.read()
        if want(batch):
            return batch
        await asyncio.sleep(0.05)
    raise AssertionError(f"{_capture(socket, 'oc-stock')}\nbatch={batch!r}")


def _contains_native_prompt(request: dict[str, object], prompt: str) -> bool:
    messages = request.get("messages")
    return isinstance(messages, list) and any(prompt in json.dumps(message) for message in messages)


async def _launch_idle_stock(
    host: FrontendRuntimeHost,
    socket: Path,
    cwd: Path,
    marker: Path,
    probe: _Probe,
    provider: _OpenAICompatibleProvider,
) -> _StockLaunch:
    manifest_runtime = MANIFEST.runtime
    assert manifest_runtime is not None
    initial = await _launch_initial(host, socket, cwd, marker, manifest_runtime, probe)
    await _until(
        provider.request_received.is_set,
        timeout=15,
        detail=lambda: f"{_capture(socket, 'oc-stock')}\n{provider.detail()}",
    )
    provider.release()
    await _until(
        provider.response_completed.is_set,
        timeout=15,
        detail=lambda: f"{_capture(socket, 'oc-stock')}\n{provider.detail()}",
    )
    await _wait_for_batch(
        probe,
        socket,
        timeout=15,
        want=lambda batch: batch.status is Status.IDLE,
    )
    return initial


@pytest.mark.skipif(not _enabled(), reason="set THEATER_OPENCODE_STOCK_CONFORMANCE=1")
async def test_stock_tui_accepts_native_send_with_exact_lineage(monkeypatch) -> None:
    binary = shutil.which("opencode")
    if binary is None or shutil.which("tmux") is None:
        pytest.skip("opencode and tmux are required")
    assert binary is not None
    version = await asyncio.to_thread(_stock_version, binary)
    if "1.18.29" not in version:
        pytest.skip(f"expected stock OpenCode 1.18.29, found {version.strip()!r}")

    root = Path(tempfile.mkdtemp(prefix="oc-stock-native-", dir="/tmp")).resolve()
    socket = root / "tmux.sock"
    marker = root / "user-plugin.log"
    host = FrontendRuntimeHost()
    probe = _Probe()
    provider = _OpenAICompatibleProvider()
    try:
        await provider.start()
        cwd = _configure_isolation(monkeypatch, root, marker, provider.base_url)
        initial = await _launch_idle_stock(host, socket, cwd, marker, probe, provider)

        # Native send over the authenticated bridge, with no keyboard input.
        runtime = probe.runtimes[-1]
        snapshot = await runtime.snapshot()
        assert snapshot.capabilities.supports(RuntimeCapability.SEND)
        assert snapshot.capabilities.supports(RuntimeCapability.QUEUE_FOLLOWUP)
        assert snapshot.native_session_id == initial.session_id
        receipt = await runtime.send(operation_id="stock-native-1", prompt="native send prompt")
        assert receipt.result is DeliveryResult.ACCEPTED, receipt.error
        assert receipt.native_turn_id is not None
        assert receipt.native_turn_id.startswith("msg_")

        # The stock TUI relayed the prompt to the model through the same session.
        await _until(
            lambda: (
                len(provider.requests) >= 2
                and _contains_native_prompt(provider.requests[-1][1], "native send prompt")
            ),
            timeout=30,
            detail=lambda: f"{_capture(socket, 'oc-stock')}\n{provider.detail()}",
        )
        await _until(
            lambda: "native send prompt" in _capture(socket, "oc-stock"),
            timeout=15,
            detail=lambda: _capture(socket, "oc-stock"),
        )

        # The exact assistant lineage terminates the submitted turn.
        batch = await _wait_for_batch(
            probe,
            socket,
            timeout=30,
            want=lambda batch: any(
                outcome.native_turn_id == receipt.native_turn_id
                for outcome in batch.terminal_evidence
            ),
        )
        outcomes = [
            outcome
            for outcome in batch.terminal_evidence
            if outcome.native_turn_id == receipt.native_turn_id
        ]
        (outcome,) = outcomes
        assert outcome.terminal is NativeTurnTerminal.COMPLETED
        assert outcome.native_session_id == initial.session_id
        assert batch.status is Status.IDLE

        # Directly: the persisted assistant info names our messageID as parent.
        assert receipt.native_turn_id in _assistant_parent_ids(probe)

        # A duplicate operation answers from the cached receipt without a
        # third model request: mutations happen exactly once.
        duplicate = await runtime.send(operation_id="stock-native-1", prompt="native send prompt")
        assert duplicate.result is DeliveryResult.ACCEPTED
        assert duplicate.native_turn_id == receipt.native_turn_id
        # Exactly-once: the cached receipt never re-mutates, though the stock
        # TUI keeps making its own background model calls (title generation).
        await asyncio.sleep(1)
        native_requests = [
            request
            for _, request in provider.requests
            if _contains_native_prompt(request, "native send prompt")
        ]
        assert len(native_requests) == 1
        assert not provider.errors, provider.detail()
        await _tmux_run(socket, "kill-session", "-t", "oc-stock")
    finally:
        await provider.aclose()
        await _cleanup(host, socket, (probe,))
        shutil.rmtree(root, ignore_errors=True)
