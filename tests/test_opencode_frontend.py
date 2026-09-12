"""Passive OpenCode TUI extension contracts."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.spawning.planning import install_frontend_plan
from theater.harness.builtin.plugins.opencode.frontend import install_opencode_tui_extension
from theater.harness.builtin.plugins.opencode.launch import plan_launch
from theater.harness.builtin.plugins.opencode.live import OpenCodeTuiLiveSource
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST
from theater.harness.builtin.plugins.opencode.runtime import OpenCodeFrontendRuntime
from theater.harness.builtin.plugins.opencode.runtime_plan import (
    OPENCODE_TUI_COMPATIBILITY_POLICY,
    probe_opencode_compatibility,
)
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.runtime import (
    DeliveryResult,
    RuntimeCapability,
    RuntimeContext,
    RuntimeFrontendConnection,
    RuntimeFrontendInstallContext,
    RuntimeHost,
    RuntimeIO,
    RuntimeNotification,
    RuntimeProbeContext,
)
from theater.models import BadRequest, Participant, Status


class _IO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float):
        del endpoint, timeout
        raise AssertionError("passive OpenCode observation never opens a backend connection")


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
    assert "api.client" not in plugin
    assert "api.client.session.prompt" not in plugin
    assert ".prompt(" not in plugin
    assert "api.ui.Slot" not in plugin
    assert "expected.writableLength" in plugin
    assert "snapshotWanted" in plugin
    assert "snapshotIntervalMs" in plugin
    assert "tokenReadTimeoutMs" in plugin
    assert "history" not in plugin
    assert "route_session_id" in plugin
    assert "session_epoch" in plugin


def test_rendered_extension_scopes_lifecycle_and_reconnects_after_host_restart(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required to execute the rendered OpenCode TUI extension")
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
    runtime = MANIFEST.runtime
    assert runtime is not None

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


async def test_runtime_receives_observation_without_offering_native_controls() -> None:
    frontend = _Frontend()
    runtime = OpenCodeFrontendRuntime(
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
    source = runtime.live_source()
    frontend.publish(
        RuntimeNotification(
            method="snapshot",
            params={**_scope(), "status": {"type": "busy"}},
        )
    )
    await asyncio.sleep(0)
    batch = await source.read()
    assert batch.status is Status.WORKING
    assert batch.trajectory == ()
    snapshot = await runtime.snapshot()
    assert not snapshot.capabilities.supports(RuntimeCapability.SEND)
    receipt = await runtime.send(operation_id="op-1", prompt="do not send")
    assert receipt.result is DeliveryResult.REJECTED
    await runtime.aclose()


def test_manifest_declares_frontend_observation_and_legacy_controls() -> None:
    runtime = MANIFEST.runtime
    assert runtime is not None
    assert runtime.host is RuntimeHost.FRONTEND
    assert runtime.plan is None
    assert runtime.legacy_fallback == {
        RuntimeCapability.SEND,
        RuntimeCapability.QUEUE_FOLLOWUP,
        RuntimeCapability.INTERRUPT,
    }
    assert runtime.unavailable_capabilities == {
        RuntimeCapability.STEER,
        RuntimeCapability.SETTINGS_UPDATE,
    }
    assert runtime.channel.drives_job_completion is False
    assert [capability.signal.value for capability in runtime.channel.channel.capabilities] == [
        "lifecycle"
    ]


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
