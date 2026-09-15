"""Stock-binary qualification proof for the detached OpenCode server topology.

Each test drives the pinned stock release (1.18.29+c470c79) exactly as the
detached topology runs it: an isolated `opencode serve --port 0` process with a
file-held Basic-auth credential, loopback HTTP, and the server runtime. The
module self-skips when the pinned binary is not on PATH. No credential ever
reaches argv, stdout, or an assertion.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import os
import secrets
import selectors
import shutil
import subprocess
import tempfile
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from theater.harness.builtin.plugins.opencode.http import OpenCodeClient
from theater.harness.builtin.plugins.opencode.server_discovery import (
    parse_server_stdout_endpoint,
)
from theater.harness.builtin.plugins.opencode.server_runtime import (
    OpenCodeServerRuntime,
)
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    DeliveryResult,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeIO,
    SessionOpenMode,
)

_PINNED_VERSION = "1.18.29+c470c79"
_PLUGIN_FIXTURE = Path(__file__).parent / "fixtures" / "opencode_probe_plugin.mjs"


def _binary_version() -> str | None:
    try:
        run = subprocess.run(
            ["opencode", "--version"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout.decode("utf-8", errors="replace").strip() or None


_BINARY_VERSION = _binary_version()
pytestmark = pytest.mark.skipif(
    _BINARY_VERSION != _PINNED_VERSION,
    reason=f"stock OpenCode {_PINNED_VERSION} not on PATH (found {_BINARY_VERSION!r}); "
    "the proof only qualifies the pinned release",
)


class _IO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float) -> Any:
        del endpoint, timeout
        raise AssertionError("the server runtime never opens a frontend connection")


class StockServer:
    """One isolated stock serve process with its private credential file."""

    def __init__(self, process: subprocess.Popen[bytes], workdir: Path, endpoint: str) -> None:
        self.process = process
        self.workdir = workdir
        self.endpoint = endpoint
        self.token_file = workdir / "runtime.token"
        self.plugin_log = workdir / "plugin-events.txt"

    def stop(self) -> None:
        self.process.terminate()
        with contextlib.suppress(subprocess.SubprocessError):
            self.process.wait(timeout=10)
        shutil.rmtree(self.workdir, ignore_errors=True)

    def password(self) -> str:
        return self.token_file.read_text(encoding="utf-8")

    def client(self) -> OpenCodeClient:
        return OpenCodeClient(endpoint=self.endpoint, token_file=self.token_file)


def _read_banner_endpoint(process: subprocess.Popen[bytes]) -> str:
    """Block until serve announces its port-0 loopback origin."""
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 20.0
    try:
        while time.monotonic() < deadline:
            if selector.select(timeout=0.2):
                line = process.stdout.readline().decode("utf-8", errors="replace")
                if not line:
                    break
                endpoint = parse_server_stdout_endpoint(line)
                if endpoint is not None:
                    return endpoint
    finally:
        selector.close()
    raise AssertionError("stock serve printed no parseable listening banner")


def _request_status(
    endpoint: str,
    method: str,
    path: str,
    *,
    password: str | None = None,
    timeout: float = 10.0,
) -> int:
    parts = urlsplit(endpoint)
    assert parts.hostname is not None and parts.port is not None
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    headers = {}
    if password is not None:
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    try:
        conn.request(method, path, headers=headers)
        return conn.getresponse().status
    finally:
        conn.close()


def _start_stock_server() -> StockServer:
    workdir = Path(tempfile.mkdtemp(prefix="theater-ocproof-"))
    password = secrets.token_urlsafe(24)
    token_file = workdir / "runtime.token"
    token_file.write_text(password, encoding="utf-8")
    token_file.chmod(0o600)
    config = workdir / "server.json"
    config.write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "model": "mistral/mistral-large-latest",
                "plugin": [str(_PLUGIN_FIXTURE)],
            }
        ),
        encoding="utf-8",
    )
    process = subprocess.Popen(
        ["opencode", "serve", "--port", "0", "--hostname", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            **os.environ,
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_DB": str(workdir / "proof.sqlite"),
            "OPENCODE_CONFIG": str(config),
            "THEATER_PROBE_PLUGIN_LOG": str(workdir / "plugin-events.txt"),
        },
    )
    server = StockServer(process, workdir, "http://127.0.0.1:1")
    endpoint = _read_banner_endpoint(process)
    server.endpoint = endpoint
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            if _request_status(endpoint, "GET", "/global/health", password=password) == 200:
                return server
        if process.poll() is not None:
            break
        time.sleep(0.2)
    server.stop()
    raise AssertionError("stock serve never answered authenticated /global/health")


@pytest.fixture(scope="session")
def stock() -> Iterator[StockServer]:
    server = _start_stock_server()
    try:
        yield server
    finally:
        server.stop()


def _context(server: StockServer) -> RuntimeContext:
    return RuntimeContext(
        participant_id="hproof0000001",
        cwd=str(server.workdir),
        io=_IO(),
        backend_generation=7,
        endpoint=server.endpoint,
        token_file=server.token_file,
    )


def _info(message: Mapping[str, object]) -> Mapping[str, object]:
    info = message.get("info")
    assert isinstance(info, Mapping), message
    return info


async def _wait_for(predicate: Callable[[], Awaitable[bool]], timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.05)


async def _wait_for_state(
    runtime: OpenCodeServerRuntime, state: RuntimeExecutionState, timeout: float
) -> None:
    async def settled() -> bool:
        return (await runtime.snapshot()).execution_state is state

    await _wait_for(settled, timeout=timeout)


async def _wait_for_health(runtime: OpenCodeServerRuntime, timeout: float = 30.0) -> None:
    async def settled() -> bool:
        return (await runtime.snapshot()).health is ConnectionHealth.CONNECTED

    await _wait_for(settled, timeout=timeout)


def _attach_denied(server: StockServer, session_id: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["opencode", "attach", server.endpoint, "--session", session_id],
        capture_output=True,
        timeout=30,
        check=False,
        env={**os.environ, "OPENCODE_SERVER_PASSWORD": "definitely-not-the-password"},
    )


def _attach_authenticated_and_alive(server: StockServer, session_id: str) -> bool:
    """Run attach on a PTY; True only when it authenticates and keeps running."""
    import pty

    master, slave = pty.openpty()
    process = subprocess.Popen(
        ["opencode", "attach", server.endpoint, "--session", session_id],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env={**os.environ, "OPENCODE_SERVER_PASSWORD": server.password()},
    )
    os.close(slave)
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        return True
    else:
        return False
    finally:
        process.terminate()
        with contextlib.suppress(subprocess.SubprocessError):
            process.wait(timeout=10)
        os.close(master)


async def test_port0_banner_and_basic_auth_reach_every_surface(stock: StockServer) -> None:
    assert await stock.client().health() == _PINNED_VERSION
    for method, path in (
        ("GET", "/global/health"),
        ("GET", "/session/status"),
        ("GET", "/event"),
        ("POST", "/session/ses_proofunauth/prompt_async"),
    ):
        status = await asyncio.to_thread(_request_status, stock.endpoint, method, path)
        assert status == 401, (method, path, status)


async def test_exact_session_identity_and_idle_absence(stock: StockServer) -> None:
    runtime = OpenCodeServerRuntime(_context(stock))
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    session_id = binding.native_session_id
    assert binding.native_version == _PINNED_VERSION
    readback = await stock.client().read_session(session_id)
    assert readback["id"] == session_id
    assert await stock.client().list_messages(session_id) == ()
    statuses = await stock.client().session_status()
    assert session_id not in statuses, "idle session must be absent from the status map"
    await runtime.aclose()


async def test_prompt_admission_lineage_busy_gate_and_release(stock: StockServer) -> None:
    runtime = OpenCodeServerRuntime(_context(stock))
    session_id = (await runtime.open_session(mode=SessionOpenMode.NEW)).native_session_id
    assert session_id is not None
    await _wait_for_health(runtime)

    first = await runtime.send(operation_id="proof-1", prompt="Reply with exactly: ok")
    assert first.result is DeliveryResult.ACCEPTED
    turn_id = first.native_turn_id
    assert isinstance(turn_id, str) and turn_id.startswith("msg_")
    snapshot = await runtime.snapshot()
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    assert snapshot.native_turn_id == turn_id

    busy = await runtime.send(operation_id="proof-2", prompt="Reply with exactly: no")
    assert busy.result is DeliveryResult.REJECTED
    assert busy.error_code == "busy"

    async def map_is_busy() -> bool:
        statuses = await stock.client().session_status()
        entry = statuses.get(session_id)
        return isinstance(entry, Mapping) and entry.get("type") == "busy"

    await _wait_for(map_is_busy, timeout=30.0)
    await _wait_for_state(runtime, RuntimeExecutionState.IDLE, timeout=90.0)
    assert (await runtime.snapshot()).native_turn_id is None
    statuses = await stock.client().session_status()
    assert session_id not in statuses, "idle session must be absent from the status map"

    infos = [_info(m) for m in await stock.client().list_messages(session_id)]
    assert any(i.get("id") == turn_id and i.get("role") == "user" for i in infos)
    assert any(i.get("role") == "assistant" and i.get("parentID") == turn_id for i in infos), (
        "no assistant message carries the minted user id as parentID"
    )

    released = await runtime.send(operation_id="proof-3", prompt="Reply with exactly: ok")
    assert released.result is DeliveryResult.ACCEPTED
    assert released.native_turn_id != turn_id
    await _wait_for_state(runtime, RuntimeExecutionState.IDLE, timeout=90.0)
    infos = [_info(m) for m in await stock.client().list_messages(session_id)]
    ids = [i.get("id") for i in infos]
    assert ids.count(released.native_turn_id) == 1
    await runtime.aclose()


async def test_sse_reconnect_and_history_readback_never_duplicate(stock: StockServer) -> None:
    runtime = OpenCodeServerRuntime(_context(stock))
    session_id = (await runtime.open_session(mode=SessionOpenMode.NEW)).native_session_id
    assert session_id is not None
    first = await runtime.send(operation_id="proof-4", prompt="Reply with exactly: ok")
    assert first.result is DeliveryResult.ACCEPTED
    await _wait_for_state(runtime, RuntimeExecutionState.IDLE, timeout=90.0)
    await runtime.aclose()

    reconnect = OpenCodeServerRuntime(_context(stock))
    binding = await reconnect.open_session(
        mode=SessionOpenMode.RECONNECT, native_session_id=session_id
    )
    assert binding.native_session_id == session_id
    await _wait_for_health(reconnect)
    assert (await reconnect.snapshot()).execution_state is RuntimeExecutionState.IDLE
    infos = [_info(m) for m in await stock.client().list_messages(session_id)]
    ids = [i.get("id") for i in infos]
    assert first.native_turn_id in ids
    assert len(ids) == len(set(ids))

    again = await reconnect.send(operation_id="proof-5", prompt="Reply with exactly: ok")
    assert again.result is DeliveryResult.ACCEPTED
    await _wait_for_state(reconnect, RuntimeExecutionState.IDLE, timeout=90.0)
    infos = [_info(m) for m in await stock.client().list_messages(session_id)]
    ids = [i.get("id") for i in infos]
    assert ids.count(again.native_turn_id) == 1, "reconnected admission duplicated history"
    await reconnect.aclose()


async def test_fork_while_parent_is_active(stock: StockServer) -> None:
    runtime = OpenCodeServerRuntime(_context(stock))
    session_id = (await runtime.open_session(mode=SessionOpenMode.NEW)).native_session_id
    assert session_id is not None
    parent = await runtime.send(operation_id="proof-6", prompt="Reply with exactly: ok")
    assert parent.result is DeliveryResult.ACCEPTED
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE

    # Stock fork copies committed history with fresh message ids; role order is
    # the observable lineage, and ids must not survive the fork.
    parent_infos = [_info(m) for m in await stock.client().list_messages(session_id)]
    parent_roles = [str(i.get("role")) for i in parent_infos]
    child = await runtime.open_session(mode=SessionOpenMode.FORK, native_session_id=session_id)
    child_id = child.native_session_id
    assert child_id is not None and child_id != session_id
    child_snapshot = await runtime.snapshot()
    assert child_snapshot.native_session_id == child_id
    assert child_snapshot.execution_state is RuntimeExecutionState.IDLE
    statuses = await stock.client().session_status()
    parent_entry = statuses.get(session_id)
    assert isinstance(parent_entry, Mapping)
    assert parent_entry.get("type") in ("busy", "retry")
    assert child_id not in statuses

    child_infos = [_info(m) for m in await stock.client().list_messages(child_id)]
    child_roles = [str(i.get("role")) for i in child_infos]
    assert child_roles[: len(parent_roles)] == parent_roles, "fork lost committed history"
    parent_ids = {str(i.get("id")) for i in parent_infos}
    assert not (parent_ids & {str(i.get("id")) for i in child_infos}), "fork must remap message ids"

    admitted = await runtime.send(operation_id="proof-7", prompt="Reply with exactly: ok")
    assert admitted.result is DeliveryResult.ACCEPTED
    await _wait_for_state(runtime, RuntimeExecutionState.IDLE, timeout=90.0)
    await runtime.aclose()


async def test_attach_authentication_and_server_survival(stock: StockServer) -> None:
    runtime = OpenCodeServerRuntime(_context(stock))
    session_id = (await runtime.open_session(mode=SessionOpenMode.NEW)).native_session_id
    assert session_id is not None
    await runtime.aclose()

    denied = await asyncio.to_thread(_attach_denied, stock, session_id)
    assert denied.returncode != 0
    assert b"401" in denied.stdout + denied.stderr

    alive = await asyncio.to_thread(_attach_authenticated_and_alive, stock, session_id)
    assert alive, "attach with the file-held credential exited before rendering"

    assert await stock.client().health() == _PINNED_VERSION
    readback = await stock.client().read_session(session_id)
    assert readback["id"] == session_id


async def test_server_mode_plugin_bus_parity(stock: StockServer) -> None:
    runtime = OpenCodeServerRuntime(_context(stock))
    session_id = (await runtime.open_session(mode=SessionOpenMode.NEW)).native_session_id
    assert session_id is not None
    assert (await stock.client().read_session(session_id))["id"] == session_id

    events = await asyncio.to_thread(_plugin_events, stock)
    assert "loaded" in events
    assert "session.created" in events
    assert "session.updated" in events
    await runtime.aclose()


def _plugin_events(server: StockServer) -> list[str]:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        lines = server.plugin_log.read_text().splitlines()
        if "session.created" in lines:
            return lines
        time.sleep(0.2)
    return server.plugin_log.read_text().splitlines()
