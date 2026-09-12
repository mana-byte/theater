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
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import RuntimeFrontendConnection, RuntimeNotification
from theater.models import Participant

pytestmark = pytest.mark.tmux


@dataclass(slots=True)
class _Probe:
    notifications: list[RuntimeNotification] = field(default_factory=list)
    connections: list[RuntimeFrontendConnection] = field(default_factory=list)
    disconnected: list[RuntimeFrontendConnection] = field(default_factory=list)
    readers: list[asyncio.Task[None]] = field(default_factory=list)

    async def consume(self, connection: RuntimeFrontendConnection) -> None:
        async for notification in connection.notifications():
            self.notifications.append(notification)

    async def on_connect(self, connection: RuntimeFrontendConnection) -> None:
        self.connections.append(connection)
        self.readers.append(asyncio.create_task(self.consume(connection)))

    async def on_disconnect(self, connection: RuntimeFrontendConnection) -> None:
        self.disconnected.append(connection)


@dataclass(frozen=True, slots=True)
class _StockLaunch:
    participant: Participant
    endpoint: str
    plan: LaunchPlan
    token: str
    session_id: str


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


def _configure_isolation(monkeypatch, root: Path, marker: Path) -> Path:
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
    _write_user_config(config)
    return cwd


def _write_user_config(config: Path) -> None:
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
                        "options": {"apiKey": "test-key", "baseURL": "http://127.0.0.1:9/v1"},
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
    plan, token = _frontend_plan(participant, runtime, endpoint, prompt="stock extension smoke")
    await _start_listener(host, participant, endpoint, token, probe)
    environment = _environment(plan, participant)
    await _launch_pane(socket, "oc-stock", cwd, plan, environment)
    await _until(
        lambda: any(item.method == "snapshot" for item in probe.notifications),
        timeout=30,
        detail=lambda: _capture(socket, "oc-stock"),
    )
    await _until(
        lambda: marker.exists() and "loaded:7" in marker.read_text(),
        timeout=15,
        detail=lambda: _capture(socket, "oc-stock"),
    )
    snapshot = next(item for item in probe.notifications if item.method == "snapshot")
    session_id = snapshot.params["session_id"]
    assert isinstance(session_id, str) and session_id
    assert "--model" in plan.argv and "--auto" in plan.argv
    return _StockLaunch(participant, endpoint, plan, token, session_id)


async def _restart_listener(
    host: FrontendRuntimeHost,
    launch: _StockLaunch,
    probe: _Probe,
    socket: Path,
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
        lambda: len(probe.connections) >= 2 and len(probe.notifications) > before_reconnect,
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
            item.method == "snapshot" and item.params.get("session_id") != parent.session_id
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
    readers = [reader for probe in probes for reader in probe.readers]
    for reader in readers:
        if not reader.done():
            reader.cancel()
    if readers:
        await asyncio.gather(*readers, return_exceptions=True)


@pytest.mark.skipif(not _enabled(), reason="set THEATER_OPENCODE_STOCK_CONFORMANCE=1")
async def test_stock_tui_loads_passive_extension_and_reconnects(monkeypatch) -> None:
    binary = shutil.which("opencode")
    if binary is None or shutil.which("tmux") is None:
        pytest.skip("opencode and tmux are required")
    version = await asyncio.to_thread(_stock_version, binary)
    if "1.18.29" not in version:
        pytest.skip(f"expected stock OpenCode 1.18.29, found {version.strip()!r}")

    root = Path(tempfile.mkdtemp(prefix="oc-stock-", dir="/tmp")).resolve()
    socket = root / "tmux.sock"
    marker = root / "user-plugin.log"
    host = FrontendRuntimeHost()
    initial_probe = _Probe()
    fork_probe = _Probe()
    try:
        cwd = _configure_isolation(monkeypatch, root, marker)
        runtime = MANIFEST.runtime
        assert runtime is not None
        initial = await _launch_initial(host, socket, cwd, marker, runtime, initial_probe)
        await _restart_listener(host, initial, initial_probe, socket)
        await _tmux_run(socket, "kill-session", "-t", "oc-stock")
        await _until(
            lambda: marker.exists() and "disposed" in marker.read_text(),
            timeout=15,
            detail=lambda: _capture(socket, "oc-stock"),
        )
        await _launch_fork(host, socket, cwd, runtime, initial, fork_probe)
        await _tmux_run(socket, "kill-session", "-t", "oc-stock-fork")
    finally:
        await _cleanup(host, socket, (initial_probe, fork_probe))
        shutil.rmtree(root, ignore_errors=True)
