"""The OpenCode TUI extension overlay and its rendered plugin."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.paths import REPO_ROOT, fixture
from theater import paths
from theater.daemon.spawning.planning import install_frontend_plan
from theater.harness.builtin.plugins.opencode.frontend import (
    install_opencode_tui_extension,
    render_opencode_tui_plugin,
)
from theater.harness.builtin.plugins.opencode.launch import plan_launch
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.runtime import (
    RuntimeFrontendInstallContext,
)
from theater.models import BadRequest, Participant


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


@pytest.mark.integration
@pytest.mark.requires_node
def test_rendered_extension_scopes_lifecycle_and_reconnects_after_host_restart(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    node = shutil.which("node")  # requires_node guarantees it
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


def test_frontend_overlay_preserves_the_ordinary_opencode_launch(
    monkeypatch, tmp_path, available_harness_binaries
) -> None:
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
    runtime = SimpleNamespace(
        frontend_installer=install_opencode_tui_extension,
        channel=SimpleNamespace(channel=SimpleNamespace(id="opencode-tui-live")),
    )

    overlay = install_frontend_plan(
        plan,
        Participant(id="open-1", harness="opencode"),
        runtime,
        "unix:///tmp/open-1.sock",
    )

    assert overlay.argv == [
        str(Path(sys.executable).resolve()),
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


@pytest.mark.integration
@pytest.mark.requires_node
def test_opencode_extension_executable_conformance(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.delenv("OPENCODE_TUI_CONFIG", raising=False)
    node = shutil.which("node")  # requires_node guarantees it
    conformance = fixture("opencode_frontend_control_conformance.mts")

    with tempfile.TemporaryDirectory(prefix="oc-control-") as raw_root:
        root = Path(raw_root)
        plugin_path = tmp_path / "theater-observer.mjs"
        plugin_path.write_text(
            render_opencode_tui_plugin(str(root / "bridge.sock"), root / "token")
        )
        result = subprocess.run(
            [
                node,
                "--experimental-strip-types",
                str(conformance),
                plugin_path.resolve().as_uri(),
                str(root),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert result.returncode == 0, f"conformance stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "opencode frontend control conformance: ok" in result.stdout
