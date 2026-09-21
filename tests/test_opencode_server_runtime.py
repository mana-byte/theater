"""The detached OpenCode server runtime against a real loopback fake server.

No transport mocking: a real TCP server streams real SSE and HTTP answers,
because admission (204 + exact id confirmation) and the never-replayed
UNKNOWN paths only mean something over a socket.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from theater.harness.builtin.plugins.opencode import server_runtime
from theater.harness.builtin.plugins.opencode.http import (
    BASIC_USERNAME,
    OpenCodeHttpError,
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

PASSWORD = "test-password-1234"
_CLOSE_STREAM = object()


class ServerFake:
    """A stock-shaped HTTP/1.1 + SSE server with recorded requests."""

    def __init__(self, *, password: str = PASSWORD) -> None:
        self._password = password
        self._server: asyncio.AbstractServer | None = None
        self.port = 0
        self.requests: list[dict[str, Any]] = []
        self.behaviors: dict[str, str] = {}
        self.sessions: dict[str, dict[str, object]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.statuses: dict[str, str] = {}
        self.inputs: dict[str, list[dict[str, object]]] = {"permission": [], "question": []}
        self.sse_events: asyncio.Queue[object] = asyncio.Queue()
        self.streams_opened = 0
        self._next = 0

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        assert self._server.sockets
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        # Never wait_closed(): a live SSE handler keeps a connection open until
        # the test loop cancels it, so waiting would hang teardown.
        if self._server is not None:
            self._server.close()

    def add_session(self, session_id: str) -> None:
        self.sessions[session_id] = {"id": session_id}
        self.messages[session_id] = []

    def push_event(self, event: dict[str, object]) -> None:
        self.sse_events.put_nowait(event)

    def push_idle(self, session_id: str) -> None:
        # Pinned shape: SessionStatus.set deletes idle sessions from the map.
        self.statuses.pop(session_id, None)
        self.push_event({"type": "session.idle", "properties": {"sessionID": session_id}})

    def close_stream(self) -> None:
        self.sse_events.put_nowait(_CLOSE_STREAM)

    def _mint_session(self) -> str:
        self._next += 1
        session_id = f"ses_fake_{self._next}"
        self.add_session(session_id)
        return session_id

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").rstrip("\r\n").split(" ")
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n"):
                    break
                name, _, value = line.decode("latin-1").rstrip("\r\n").partition(":")
                headers[name.strip().lower()] = value.strip()
            body = b""
            if "content-length" in headers:
                body = await reader.readexactly(int(headers["content-length"]))
            record = {
                "method": parts[0],
                "path": parts[1] if len(parts) > 1 else "",
                "headers": headers,
                "body": body,
            }
            self.requests.append(record)
            await self._route(record, writer, body)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass

    def _authorized(self, record: dict[str, Any]) -> bool:
        expected = base64.b64encode(f"{BASIC_USERNAME}:{self._password}".encode()).decode("ascii")
        return record["headers"].get("authorization") == f"Basic {expected}"

    async def _send(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        reason: str,
        body: bytes = b"",
        content_type: str = "application/json",
    ) -> None:
        head = f"HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\n"
        if body or status != 204:
            head += f"Content-Length: {len(body)}\r\n"
        head += "Connection: close\r\n\r\n"
        writer.write(head.encode("ascii"))
        if body:
            writer.write(body)
        await writer.drain()
        writer.close()

    async def _route(
        self, record: dict[str, Any], writer: asyncio.StreamWriter, body: bytes
    ) -> None:
        if not self._authorized(record):
            await self._send(writer, 401, "Unauthorized", b"{}")
            return
        path = record["path"]
        if path == "/event":
            await self._route_event(writer)
            return
        if path == "/global/health":
            if self.behaviors.get("health") == "fail":
                await self._send(writer, 500, "Internal Server Error", b"{}")
                return
            version = self.behaviors.get("health-version", "1.18.29")
            payload = json.dumps({"healthy": True, "version": version}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) == 1 and segments[0] in self.inputs and record["method"] == "GET":
            await self._send(writer, 200, "OK", json.dumps(self.inputs[segments[0]]).encode())
            return
        if segments == ["session"] and record["method"] == "POST":
            session_id = self._mint_session()
            payload = json.dumps({"id": session_id}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if segments == ["session", "status"] and record["method"] == "GET":
            await self._route_status(writer)
            return
        if len(segments) >= 2 and segments[0] == "session":
            await self._route_session(writer, segments, segments[1], record, body)
            return
        await self._send(writer, 404, "Not Found", b"{}")

    async def _route_session(
        self,
        writer: asyncio.StreamWriter,
        segments: list[str],
        session_id: str,
        record: dict[str, Any],
        body: bytes,
    ) -> None:
        if segments[2:] == ["children"] and record["method"] == "GET":
            children = [row for row in self.sessions.values() if row.get("parentID") == session_id]
            await self._send(writer, 200, "OK", json.dumps(children).encode())
            return
        if segments[2:] == ["fork"] and record["method"] == "POST":
            if session_id not in self.sessions:
                await self._send(writer, 404, "Not Found", b"{}")
                return
            child = self._mint_session()
            payload = json.dumps({"id": child}).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if segments[2:] == ["message"]:
            if session_id not in self.sessions:
                await self._send(writer, 404, "Not Found", b"{}")
                return
            payload = json.dumps(self.messages[session_id]).encode()
            await self._send(writer, 200, "OK", payload)
            return
        if segments[2:] == ["prompt_async"] and record["method"] == "POST":
            if session_id not in self.sessions:
                await self._send(writer, 404, "Not Found", b"{}")
                return
            await self._route_prompt(writer, session_id, body)
            return
        if len(segments) == 2 and record["method"] == "GET":
            if session_id not in self.sessions:
                payload = json.dumps({"name": "NotFoundError"}).encode()
                await self._send(writer, 404, "Not Found", payload)
                return
            if self.behaviors.get("readback") == "swap":
                payload = json.dumps({"id": "ses_somebody_else"}).encode()
                await self._send(writer, 200, "OK", payload)
                return
            payload = json.dumps(self.sessions[session_id]).encode()
            await self._send(writer, 200, "OK", payload)
            return
        await self._send(writer, 404, "Not Found", b"{}")

    async def _route_status(self, writer: asyncio.StreamWriter) -> None:
        behavior = self.behaviors.get("status")
        if behavior == "fail":
            await self._send(writer, 500, "Internal Server Error", b"{}")
            return
        payload: dict[str, object]
        if behavior == "malformed-top":
            payload = {"kind": "broken"}
        elif behavior == "malformed-target":
            payload = {"ses_parent": 42}
        elif behavior == "unknown-type":
            payload = {"ses_parent": {"type": "bananas"}}
        elif behavior == "foreign":
            payload = {"ses_somebody_else": {"type": "busy"}}
        else:
            payload = {sid: {"type": value} for sid, value in self.statuses.items()}
        await self._send(writer, 200, "OK", json.dumps(payload).encode())

    def _record_user(self, session_id: str, message_id: str, *, echo: bool) -> None:
        self.messages[session_id].append({"info": {"id": message_id, "role": "user"}, "parts": []})
        if echo:
            self.push_event(
                {
                    "type": "message.updated",
                    "properties": {
                        "sessionID": session_id,
                        "info": {"id": message_id, "role": "user"},
                    },
                }
            )

    async def _prompt_drop(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        writer.close()

    async def _prompt_500(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        await self._send(writer, 500, "Internal Server Error", b"{}")

    async def _prompt_400(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        await self._send(writer, 400, "Bad Request", b"{}")

    async def _prompt_body(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        await self._send(writer, 200, "OK", json.dumps({"queued": True}).encode())

    async def _prompt_404(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        await self._send(writer, 404, "Not Found", b"{}")

    async def _prompt_fast(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        # The turn completes before the 204 reaches the client.
        self.statuses.pop(session_id, None)
        if isinstance(message_id, str):
            self._record_user(session_id, message_id, echo=True)
        self.push_event({"type": "session.idle", "properties": {"sessionID": session_id}})
        await asyncio.sleep(0.05)
        await self._send(writer, 204, "No Content", b"")

    async def _prompt_ok_wrong_role(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        if isinstance(message_id, str):
            self.messages[session_id].append(
                {"info": {"id": message_id, "role": "assistant"}, "parts": []}
            )
        await self._send(writer, 204, "No Content", b"")

    async def _prompt_stale_idle(
        self, writer: asyncio.StreamWriter, session_id: str, message_id: str | None
    ) -> None:
        # A stale idle from a previous turn lands before this turn's echo.
        self.push_event({"type": "session.idle", "properties": {"sessionID": session_id}})
        await asyncio.sleep(0.02)
        if isinstance(message_id, str):
            self._record_user(session_id, message_id, echo=True)
            await asyncio.sleep(0.02)
        self.statuses[session_id] = "busy"
        await self._send(writer, 204, "No Content", b"")

    _PROMPT_BEHAVIORS: ClassVar[dict[str, Any]] = {
        "drop": _prompt_drop,
        "500": _prompt_500,
        "400": _prompt_400,
        "body": _prompt_body,
        "404": _prompt_404,
        "fast": _prompt_fast,
        "ok-wrong-role": _prompt_ok_wrong_role,
        "stale-idle": _prompt_stale_idle,
    }

    async def _route_prompt(
        self, writer: asyncio.StreamWriter, session_id: str, body: bytes
    ) -> None:
        behavior = self.behaviors.get("prompt", "ok")
        try:
            parsed = json.loads(body)
            message_id = parsed["messageID"] if isinstance(parsed, dict) else None
        except ValueError:
            message_id = None
        handler = self._PROMPT_BEHAVIORS.get(behavior)
        if handler is not None:
            await handler(self, writer, session_id, message_id)
            return
        # Admission: the user message becomes durable API state before 204.
        if behavior != "ok-no-record" and isinstance(message_id, str):
            self._record_user(session_id, message_id, echo=behavior == "ok")
        self.push_event(
            {
                "type": "session.status",
                "properties": {"sessionID": session_id, "status": {"type": "busy"}},
            }
        )
        self.statuses[session_id] = "busy"
        await self._send(writer, 204, "No Content", b"")

    async def _route_event(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
        )
        writer.write(
            b"data: "
            + json.dumps({"type": "server.connected", "properties": {}}).encode()
            + b"\n\n"
        )
        await writer.drain()
        self.streams_opened += 1
        while True:
            try:
                event = self.sse_events.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.005)
                continue
            if event is _CLOSE_STREAM:
                break
            writer.write(b"data: " + json.dumps(event).encode() + b"\n\n")
            try:
                await writer.drain()
            except ConnectionError:
                return
        writer.close()


class _IO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float) -> Any:
        del endpoint, timeout
        raise AssertionError("the server runtime never opens a frontend connection")


def _context(server: ServerFake, token_file: Path) -> RuntimeContext:
    return RuntimeContext(
        participant_id="h00000000001",
        cwd="/tmp",
        io=_IO(),
        backend_generation=3,
        endpoint=server.endpoint,
        token_file=token_file,
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


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never became true"
        await asyncio.sleep(0.005)


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
    await _wait_until(lambda: server.streams_opened >= 1)
    return runtime, binding


# ---- construction -------------------------------------------------------


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


# ---- session lifecycle ---------------------------------------------------


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
    server.behaviors["health-version"] = "1.18.30"
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


# ---- send admission -------------------------------------------------------


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
    await _wait_until(lambda: server.streams_opened >= 2)
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


# ---- policy-gated surfaces ------------------------------------------------


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
        await _wait_until(lambda: source.pending_inputs.awaiting)
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
        await _wait_until(lambda: source.pending_inputs.accepts("ses_child"))
        server.push_idle("ses_waiting")
        await _wait_until(lambda: source.current_execution_state() is RuntimeExecutionState.IDLE)
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
        await _wait_until(lambda: not source.pending_inputs.awaiting)
        assert (await source.read()).status is Status.IDLE

        server.inputs["question"] = [server.inputs["question"][1]]
        server.close_stream()
        await _wait_until(lambda: server.streams_opened >= 2)
        await _wait_for_health(runtime, ConnectionHealth.CONNECTED)
        await _wait_until(lambda: not source.pending_inputs.awaiting)
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
    runtime, _ = await _open_new(server, token_file)

    with pytest.raises(ValueError, match="session id"):
        await runtime.frontend_plan(native_session_id=None)
    with pytest.raises(ValueError, match="session id"):
        await runtime.frontend_plan(native_session_id="  ")

    plan = await runtime.frontend_plan(native_session_id="ses_attach")
    assert plan.argv == ["opencode", "attach", server.endpoint, "--session", "ses_attach"]
    assert plan.secret_env == {SERVER_SECRET_ENV: token_file}
    await runtime.aclose()
