"""OpenCode TUI extension contracts."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.spawning.planning import install_frontend_plan
from theater.harness.builtin.plugins.opencode.frontend import (
    install_opencode_tui_extension,
    render_opencode_tui_plugin,
)
from theater.harness.builtin.plugins.opencode.launch import plan_launch
from theater.harness.builtin.plugins.opencode.live import OpenCodeTuiLiveSource
from theater.harness.builtin.plugins.opencode.manifest import (
    _OPENCODE_TUI_LIVE,
    MANIFEST,
)
from theater.harness.builtin.plugins.opencode.runtime import (
    OpenCodeFrontendRuntime,
    opencode_frontend_runtime_factory,
)
from theater.harness.builtin.plugins.opencode.runtime_plan import (
    OPENCODE_TUI_COMPATIBILITY_POLICY,
    probe_opencode_compatibility,
)
from theater.harness.builtin.plugins.opencode.server_discovery import (
    parse_server_stdout_endpoint,
)
from theater.harness.builtin.plugins.opencode.server_plan import (
    plan_opencode_server,
    probe_opencode_server_compatibility,
)
from theater.harness.builtin.plugins.opencode.server_runtime import (
    opencode_server_runtime_factory,
)
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    DeliveryResult,
    NativeTurnTerminal,
    RuntimeCapability,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeFrontendConnection,
    RuntimeFrontendInstallContext,
    RuntimeHost,
    RuntimeIO,
    RuntimeManifest,
    RuntimeNotification,
    RuntimeProbeContext,
    RuntimeRequestError,
    RuntimeRequestTimeout,
    RuntimeSessionOrder,
)
from theater.models import BadRequest, Participant, Status


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


def test_extension_overlay_uses_only_the_passive_public_tui_surface(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    context = RuntimeFrontendInstallContext(
        participant_id="open-1",
        endpoint="unix:///tmp/open-1.sock",
        token_file=tmp_path / "receipt-token",
    )

    overlay = install_opencode_tui_extension(context)

    config_path = paths.participant_observation_dir("open-1", "opencode") / "theater-tui.json"
    plugin_path = paths.participant_observation_dir("open-1", "opencode") / "theater-observer.mjs"
    assert overlay.env == {"OPENCODE_TUI_CONFIG": str(config_path)}
    assert json.loads(overlay.files[config_path]) == {"plugin": [plugin_path.resolve().as_uri()]}
    plugin = overlay.files[plugin_path]
    assert "api.event.on" in plugin
    assert "api.state.session" in plugin
    assert "api.route.current" in plugin
    assert "api.lifecycle.onDispose" in plugin
    assert "export default" in plugin
    assert 'id: "theater.opencode.observer"' in plugin
    assert "api.client.session.promptAsync" in plugin
    # Every other SDK mutation stays out: promptAsync is the one approved send
    # path; abort and command APIs are not used.
    assert "session.abort" not in plugin
    assert ".command(" not in plugin
    assert "api.ui.Slot" not in plugin
    assert "expected.writableLength" in plugin
    assert "snapshotWanted" in plugin
    assert "snapshotIntervalMs" in plugin
    assert "tokenReadTimeoutMs" in plugin
    assert "history" not in plugin
    assert "route_session_id" in plugin
    assert "session_epoch" in plugin
    assert "operation_id" in plugin


def test_rendered_extension_scopes_lifecycle_and_reconnects_after_host_restart(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the rendered OpenCode TUI extension")
    assert node is not None
    socket_path = Path("/tmp") / f"theater-opencode-{tmp_path.name}.sock"
    token_path = tmp_path / "token"
    plugin_path = tmp_path / "theater-observer.mjs"
    driver_path = tmp_path / "driver.mjs"
    token_path.write_text("secret\n")
    plugin_path.write_text(
        install_opencode_tui_extension(
            RuntimeFrontendInstallContext(
                participant_id="open-1",
                endpoint=f"unix://{socket_path}",
                token_file=token_path,
            )
        ).files[paths.participant_observation_dir("open-1", "opencode") / "theater-observer.mjs"]
    )
    driver_path.write_text(
        f"""
import net from "node:net"
import plugin from {json.dumps(plugin_path.resolve().as_uri())}

const socketPath = {json.dumps(str(socket_path))}
const frames = []
const handlers = new Map()
let activeSession = "ses-1"
let dispose
let server
let connections = new Set()
let connectionCount = 0
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const waitFor = async (predicate) => {{
  for (let i = 0; i < 500; i += 1) {{
    if (predicate()) return
    await sleep(10)
  }}
  throw new Error("timed out waiting for rendered extension")
}}
const listen = async () => {{
  server = net.createServer((connection) => {{
    connectionCount += 1
    connections.add(connection)
    let buffer = ""
    connection.setEncoding("utf8")
    connection.on("data", (chunk) => {{
      buffer += chunk
      for (;;) {{
        const newline = buffer.indexOf("\\n")
        if (newline < 0) return
        frames.push(JSON.parse(buffer.slice(0, newline)))
        buffer = buffer.slice(newline + 1)
      }}
    }})
    connection.on("close", () => connections.delete(connection))
  }})
  await new Promise((resolve, reject) => {{
    server.once("error", reject)
    server.listen(socketPath, resolve)
  }})
}}
const stop = async () => {{
  for (const connection of connections) connection.destroy()
  await new Promise((resolve) => server.close(resolve))
}}
await listen()
const api = {{
  route: {{
    get current() {{
      if (activeSession === null) return {{ name: "home" }}
      return {{ name: "session", params: {{ sessionID: activeSession }} }}
    }},
  }},
  state: {{
    session: {{
      status: () => ({{ type: activeSession === "ses-2" ? "idle" : "busy" }}),
      messages: () => [],
      permission: () => [],
      question: () => [],
    }},
  }},
  event: {{
    on: (name, handler) => {{
      handlers.set(name, handler)
      return () => handlers.delete(name)
    }},
  }},
  lifecycle: {{
    onDispose: (handler) => {{ dispose = handler }},
  }},
}}
await plugin.tui(api)
await waitFor(() =>
  frames.some((frame) => frame.type === "snapshot" && frame.session_id === "ses-1")
)
activeSession = "ses-2"
await waitFor(() =>
  frames.some((frame) => frame.type === "snapshot" && frame.session_id === "ses-2")
)
handlers.get("session.status")({{
  id: "other",
  type: "session.status",
  properties: {{ sessionID: "ses-other", status: {{ type: "busy" }} }},
}})
handlers.get("session.status")({{
  id: "current",
  type: "session.status",
  properties: {{ sessionID: "ses-2", status: {{ type: "idle" }} }},
}})
await waitFor(() => frames.some((frame) => frame.type === "event" && frame.event?.id === "current"))
activeSession = null
await waitFor(() => frames.some((frame) => frame.type === "snapshot" && frame.session_id === null))
activeSession = "ses-2"
await stop()
await waitFor(() => connections.size === 0)
await listen()
await waitFor(() => connectionCount === 2)
await waitFor(() => frames.filter((frame) => frame.type === "snapshot").length >= 3)
dispose()
await waitFor(() => connections.size === 0)
await stop()
console.log(JSON.stringify(frames))
"""
    )
    try:
        result = subprocess.run(
            [node, str(driver_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    finally:
        socket_path.unlink(missing_ok=True)
    assert result.returncode == 0, result.stderr
    frames = json.loads(result.stdout.splitlines()[-1])
    snapshots = [frame for frame in frames if frame.get("type") == "snapshot"]
    assert frames[0]["type"] == "hello"
    assert {frame["session_id"] for frame in snapshots} == {"ses-1", "ses-2", None}
    assert all(frame["route_session_id"] == frame["session_id"] for frame in snapshots)
    events = [frame for frame in frames if frame.get("type") == "event"]
    assert [frame["event"]["id"] for frame in events] == ["current"]
    assert events[0]["route_session_id"] == "ses-2"
    assert not any(frame.get("type") == "history" for frame in frames)


def test_extension_keeps_an_explicit_user_tui_config_untouched(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.setenv("OPENCODE_TUI_CONFIG", str(tmp_path / "user-tui.json"))

    with pytest.raises(BadRequest, match="OPENCODE_TUI_CONFIG"):
        install_opencode_tui_extension(
            RuntimeFrontendInstallContext(
                participant_id="open-1",
                endpoint="unix:///tmp/open-1.sock",
                token_file=tmp_path / "receipt-token",
            )
        )


def test_frontend_overlay_preserves_the_ordinary_opencode_launch(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    plan = plan_launch(
        LaunchContext(
            participant_id="open-1",
            prompt="",
            config_path=tmp_path / "opencode.json",
            approval="yolo",
            model="provider/model",
            resume="ses-parent",
        )
    )
    runtime = RuntimeManifest(
        probe=probe_opencode_compatibility,
        plan=None,
        factory=opencode_frontend_runtime_factory,
        channel=_OPENCODE_TUI_LIVE,
        host=RuntimeHost.FRONTEND,
        frontend_installer=install_opencode_tui_extension,
    )

    overlay = install_frontend_plan(
        plan,
        Participant(id="open-1", harness="opencode"),
        runtime,
        "unix:///tmp/open-1.sock",
    )

    assert overlay.argv == [
        "opencode",
        "--model",
        "provider/model",
        "--auto",
        "-s",
        "ses-parent",
        "--fork",
    ]
    assert overlay.env["OPENCODE_DB"] == plan.env["OPENCODE_DB"]
    assert overlay.env["OPENCODE_CONFIG"] == plan.env["OPENCODE_CONFIG"]
    assert set(plan.files).issubset(overlay.files)


def _scope(session_id: str = "ses-1", epoch: int = 1) -> dict[str, object]:
    return {
        "session_id": session_id,
        "route_session_id": session_id,
        "session_epoch": epoch,
    }


async def test_live_source_rejects_message_and_part_payloads() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(
        RuntimeNotification(
            method="history",
            params={
                **_scope(),
                "messages": [
                    {
                        "info": {
                            "id": "msg-1",
                            "sessionID": "ses-1",
                            "role": "assistant",
                        },
                        "parts": [{"id": "tool-1", "sessionID": "ses-1", "type": "tool"}],
                    }
                ],
            },
        )
    )
    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **_scope(),
                "event": {
                    "id": "event-1",
                    "type": "message.part.updated",
                    "properties": {
                        "part": {
                            "id": "part-1",
                            "sessionID": "ses-1",
                            "messageID": "msg-1",
                            "type": "text",
                            "text": "one",
                        }
                    },
                },
            },
        )
    )
    batch = await source.read()
    assert batch.trajectory == ()
    assert batch.status is None
    assert batch.terminal_evidence == ()
    assert batch.progressed is False


async def test_live_source_maps_public_tui_status_without_completion() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope(), "status": {"type": "busy"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.WORKING
    assert batch.trajectory == ()
    assert batch.terminal_evidence == ()

    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope(), "status": {"type": "busy"}},
        )
    )
    duplicate = await source.read()
    assert duplicate.status is Status.WORKING
    assert duplicate.progressed is False

    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **_scope(),
                "event": {
                    "id": "status-1",
                    "type": "session.status",
                    "properties": {"sessionID": "ses-1", "status": {"type": "idle"}},
                },
            },
        )
    )
    batch = await source.read()
    assert batch.status is Status.IDLE
    assert batch.trajectory == ()

    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope(), "status": {"type": "busy"}},
        )
    )
    assert (await source.read()).status is Status.WORKING

    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **_scope(),
                "event": {
                    "id": "subagent-status-1",
                    "type": "session.status",
                    "properties": {"sessionID": "ses-child", "status": {"type": "busy"}},
                },
            },
        )
    )
    assert (await source.read()).status is Status.WORKING


async def test_live_source_rejects_foreign_sessions_and_stale_epochs() -> None:
    current = {"session_id": "ses-1"}
    source = OpenCodeTuiLiveSource(lambda: current["session_id"])
    source.feed(
        RuntimeNotification(
            method="event",
            params={
                **_scope(),
                "event": {
                    "id": "child-status",
                    "type": "session.status",
                    "properties": {"sessionID": "ses-child", "status": {"type": "busy"}},
                },
            },
        )
    )
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-1", 2), "status": {"type": "busy"}},
        )
    )
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-1", 1), "status": {"type": "idle"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.WORKING
    assert batch.trajectory == ()

    current["session_id"] = "ses-new"
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-new", 3), "status": {"type": "idle"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.IDLE
    assert batch.trajectory == ()


async def test_live_source_uses_latest_status_and_discards_it_when_identity_changes() -> None:
    current = {"session_id": "ses-a"}
    source = OpenCodeTuiLiveSource(lambda: current["session_id"])
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-a"), "status": {"type": "busy"}},
        )
    )
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-a"), "status": {"type": "idle"}},
        )
    )
    assert (await source.read()).status is Status.IDLE

    current["session_id"] = "ses-b"
    assert (await source.read()).status is None
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-b", 2), "status": {"type": "idle"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.IDLE
    assert batch.trajectory == ()


async def test_live_source_rechecks_identity_before_returning_status() -> None:
    trusted = ["ses-1"]
    source = OpenCodeTuiLiveSource(lambda: trusted[0])
    source.feed(
        RuntimeNotification(
            method="snapshot",
            params={**_scope("ses-1"), "status": {"type": "busy"}},
        )
    )
    batch = await source.read()
    assert batch.status is Status.WORKING
    trusted[0] = "ses-2"
    batch = source.validate_enrichment_batch(batch)
    assert batch.status is None
    assert batch.progressed is False


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


def _status_snapshot(
    *,
    status: str = "idle",
    session_id: str = "ses-1",
    epoch: int = 1,
) -> RuntimeNotification:
    return RuntimeNotification(
        method="snapshot",
        params={
            "session_id": session_id,
            "route_session_id": session_id,
            "session_epoch": epoch,
            "status": {"type": status},
        },
    )


def _message_event(
    info: dict[str, object],
    *,
    session_id: str = "ses-1",
    epoch: int = 1,
) -> RuntimeNotification:
    return RuntimeNotification(
        method="event",
        params={
            "session_id": session_id,
            "route_session_id": session_id,
            "session_epoch": epoch,
            "event": {
                "id": "event-message",
                "type": "message.updated",
                "properties": {"sessionID": session_id, "info": info},
            },
        },
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
    frontend.publish(_status_snapshot())
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
        _message_event(
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
    frontend.publish(_status_snapshot())
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
    frontend.publish(_status_snapshot())
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
    frontend.publish(_status_snapshot())
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
    frontend.publish(_status_snapshot(status="busy"))
    await asyncio.sleep(0.01)
    await source.read()
    receipt = await runtime.send(operation_id="op-1", prompt="hello there")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "busy"
    assert frontend.requests == []

    # Idle but an unusable prompt never reaches the plugin.
    frontend.publish(_status_snapshot())
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
    frontend.publish(_status_snapshot())
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
        frontend.publish(_status_snapshot(epoch=2))
        await asyncio.sleep(0.01)

    frontend = _ScriptedFrontend(reply=_accepted_reply(), before_reply=switch_route)
    runtime = _runtime(frontend)
    source = runtime.live_source()
    frontend.publish(_status_snapshot())
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

    frontend.publish(_status_snapshot(status="busy"))
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
            _message_event(
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
    frontend.publish(_status_snapshot())
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
        _message_event(
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
    frontend.publish(_status_snapshot())
    await asyncio.sleep(0.01)
    await source.read()

    await frontend.aclose()
    await asyncio.sleep(0.01)

    receipt = await runtime.send(operation_id="op-1", prompt="hello there")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "not_ready"
    assert frontend.requests == []
    await runtime.aclose()


def test_manifest_declares_the_detached_server_runtime() -> None:
    runtime = MANIFEST.runtime
    assert runtime is not None
    assert runtime.host is RuntimeHost.DETACHED_BACKEND
    assert runtime.plan is plan_opencode_server
    assert runtime.factory is opencode_server_runtime_factory
    assert runtime.probe is probe_opencode_server_compatibility
    assert runtime.session_order is RuntimeSessionOrder.SESSION_FIRST
    assert runtime.frontend_installer is None
    assert runtime.legacy_fallback == frozenset({RuntimeCapability.INTERRUPT})
    assert runtime.unavailable_capabilities == {
        RuntimeCapability.STEER,
        RuntimeCapability.SETTINGS_UPDATE,
    }
    assert runtime.channel.channel.id == "opencode-server-live"
    assert runtime.channel.drives_job_completion is False
    assert [capability.signal.value for capability in runtime.channel.channel.capabilities] == [
        "lifecycle"
    ]
    assert runtime.runtime_credential is not None
    assert runtime.runtime_credential.env == ("OPENCODE_SERVER_PASSWORD",)
    assert runtime.endpoint_discovery is not None
    assert runtime.endpoint_discovery.parser is parse_server_stdout_endpoint


def test_probe_accepts_the_declared_release_range(monkeypatch) -> None:
    class _Result:
        def __init__(self, output: str) -> None:
            self.returncode = 0
            self.stdout = output
            self.stderr = ""

    def run(argv, **kwargs):
        del kwargs
        return _Result("1.18.29") if argv[-1] == "--version" else _Result("--model --auto --fork")

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.opencode.runtime_plan.subprocess.run",
        run,
    )

    compatibility = probe_opencode_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is True
    assert compatibility.policy == OPENCODE_TUI_COMPATIBILITY_POLICY


def test_probe_rejects_a_prerelease(monkeypatch) -> None:
    class _Result:
        returncode = 0
        stdout = "1.18.29-beta.1"
        stderr = ""

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.opencode.runtime_plan.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )

    compatibility = probe_opencode_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False


def test_probe_rejects_releases_outside_the_tested_window(monkeypatch) -> None:
    class _Result:
        def __init__(self, output: str) -> None:
            self.returncode = 0
            self.stdout = output
            self.stderr = ""

    def run(argv, **kwargs):
        del kwargs
        return _Result("1.19.0") if argv[-1] == "--version" else _Result("--model --auto --fork")

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.opencode.runtime_plan.subprocess.run",
        run,
    )

    compatibility = probe_opencode_compatibility(RuntimeProbeContext(binary="opencode"))
    assert compatibility.supported is False
    assert compatibility.policy == OPENCODE_TUI_COMPATIBILITY_POLICY
    assert "1.18.29" in (compatibility.reason or "")


def test_opencode_extension_executable_conformance(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the rendered OpenCode TUI extension")
    assert node is not None
    fixture = Path(__file__).parent / "fixtures" / "opencode_frontend_control_conformance.mts"

    with tempfile.TemporaryDirectory(prefix="oc-control-") as raw_root:
        root = Path(raw_root)
        plugin_path = tmp_path / "theater-observer.mjs"
        plugin_path.write_text(
            render_opencode_tui_plugin(str(root / "bridge.sock"), root / "token")
        )
        result = subprocess.run(
            [
                node,
                "--experimental-transform-types",
                str(fixture),
                plugin_path.resolve().as_uri(),
                str(root),
            ],
            cwd=Path(__file__).parents[1],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert result.returncode == 0, f"conformance stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "opencode frontend control conformance: ok" in result.stdout


async def test_live_source_stages_interrupted_and_failed_lineage_only_for_submitted_turns() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(_status_snapshot())
    assert (await source.read()).status is Status.IDLE

    assert source.note_submitted_turn("ses-1", 1, "msg_user_1") is True
    source.feed(
        _message_event(
            {
                "id": "msg_asst_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_1",
                "error": {"name": "MessageAbortedError"},
            }
        )
    )
    batch = await source.read()
    (outcome,) = batch.terminal_evidence
    assert outcome.native_turn_id == "msg_user_1"
    assert outcome.terminal is NativeTurnTerminal.INTERRUPTED
    assert outcome.error_code == "MessageAbortedError"
    source.terminal_evidence_delivered()

    # A failure without the abort marker is FAILED, with the error name kept.
    assert source.note_submitted_turn("ses-1", 1, "msg_user_2") is True
    source.feed(
        _message_event(
            {
                "id": "msg_asst_2",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_2",
                "error": {"name": "ProviderError"},
            }
        )
    )
    batch = await source.read()
    (outcome,) = batch.terminal_evidence
    assert outcome.native_turn_id == "msg_user_2"
    assert outcome.terminal is NativeTurnTerminal.FAILED
    assert outcome.error_code == "ProviderError"
    source.terminal_evidence_delivered()


async def test_live_source_ignores_lineage_without_a_submitted_turn() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(_status_snapshot())
    assert (await source.read()).status is Status.IDLE

    source.feed(
        _message_event(
            {
                "id": "msg_asst_unknown",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_never_submitted",
                "time": {"completed": 1500},
            }
        )
    )
    # A foreign-session event is out of scope entirely.
    source.feed(
        _message_event(
            {
                "id": "msg_asst_foreign",
                "role": "assistant",
                "sessionID": "ses-other",
                "parentID": "msg_user_1",
                "time": {"completed": 1500},
            },
            session_id="ses-1",
        )
    )
    batch = await source.read()
    assert batch.terminal_evidence == ()

    # Once submitted, the same turn's terminal evidence is staged once only.
    assert source.note_submitted_turn("ses-1", 1, "msg_user_1") is True
    source.feed(
        _message_event(
            {
                "id": "msg_asst_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_1",
                "time": {"completed": 1500},
            }
        )
    )
    source.feed(
        _message_event(
            {
                "id": "msg_asst_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_1",
                "time": {"completed": 1600},
            }
        )
    )
    batch = await source.read()
    (outcome,) = batch.terminal_evidence
    assert outcome.native_turn_id == "msg_user_1"
    assert outcome.completed_at == 1.5


async def test_live_source_drops_unsubmitted_turns_when_the_scope_moves() -> None:
    source = OpenCodeTuiLiveSource(lambda: "ses-1")
    source.feed(_status_snapshot())
    assert (await source.read()).status is Status.IDLE

    assert source.note_submitted_turn("ses-1", 1, "msg_user_1") is True
    # The route moves before the turn's lineage arrives.
    source.feed(_status_snapshot(epoch=2))
    assert source.note_submitted_turn("ses-1", 2, "msg_user_2") is True
    assert source.active_turn_id() == "msg_user_2"

    # The earlier epoch's lineage must not complete the newer turn.
    source.feed(
        _message_event(
            {
                "id": "msg_asst_1",
                "role": "assistant",
                "sessionID": "ses-1",
                "parentID": "msg_user_1",
                "time": {"completed": 1500},
            },
            epoch=1,
        )
    )
    batch = await source.read()
    assert batch.terminal_evidence == ()
    assert source.active_turn_id() == "msg_user_2"
