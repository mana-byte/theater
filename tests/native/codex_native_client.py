"""Test-only client for an installed, unmodified Codex app-server.

This module implements just enough of the frozen Theater topology to prove it
against a real release:

* ``codex app-server --listen unix://<private-socket>`` — detached backend.
* WebSocket frames with an HTTP Upgrade handshake over that Unix socket
  (RFC 6455), carrying JSON-RPC-shaped messages that omit the ``jsonrpc``
  version field.
* ``codex --remote unix://<socket> resume <thread-id>`` — the native CLI UI.

Everything is stdlib-only, bounded, and deterministic in cleanup. It is a
test helper: no production code may import it.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import signal
import socket
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_BUFFERED_MESSAGES = 512
HANDSHAKE_TIMEOUT = 20.0
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class NativeProtocolError(RuntimeError):
    """The app-server violated the framing or handshake this helper implements."""


class FrameTooLarge(NativeProtocolError):
    """A single WebSocket frame exceeded ``MAX_FRAME_BYTES``."""


def wait_until(predicate, *, timeout, poll=0.2, what=None):
    """Deadline-bounded state polling; never a blind fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    if what is not None:
        msg = f"timed out after {timeout}s waiting for {what}"
        raise TimeoutError(msg)
    return False


def build_client_handshake(path: str = "/") -> tuple[bytes, str]:
    """Return (request-bytes, sec-websocket-key) for the HTTP Upgrade."""
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    return request.encode(), key


def expected_accept(key: str) -> str:
    digest = hashlib.sha1((key + WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def encode_frame(payload: bytes, opcode: int = OP_TEXT, *, mask: bool = True) -> bytes:
    """Encode one RFC 6455 frame. Client frames must be masked."""
    header = bytearray()
    header.append(0x80 | opcode)
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        header.append(mask_bit | length)
    elif length < 65536:
        header.append(mask_bit | 126)
        header += struct.pack(">H", length)
    else:
        header.append(mask_bit | 127)
        header += struct.pack(">Q", length)
    if mask:
        mask_key = secrets.token_bytes(4)
        header += mask_key
        payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))
    return bytes(header) + payload


def decode_frames(buffer: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Decode every complete frame in ``buffer``; return (frames, remainder)."""
    frames: list[tuple[int, bytes]] = []
    offset = 0
    while True:
        if len(buffer) - offset < 2:
            break
        b1 = buffer[offset]
        b2 = buffer[offset + 1]
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        masked = bool(b2 & 0x80)
        cursor = offset + 2
        if length == 126:
            if len(buffer) - cursor < 2:
                break
            length = struct.unpack(">H", buffer[cursor : cursor + 2])[0]
            cursor += 2
        elif length == 127:
            if len(buffer) - cursor < 8:
                break
            length = struct.unpack(">Q", buffer[cursor : cursor + 8])[0]
            cursor += 8
        if length > MAX_FRAME_BYTES:
            msg = f"frame of {length} bytes exceeds bound {MAX_FRAME_BYTES}"
            raise FrameTooLarge(msg)
        mask_key = b""
        if masked:
            if len(buffer) - cursor < 4:
                break
            mask_key = buffer[cursor : cursor + 4]
            cursor += 4
        if len(buffer) - cursor < length:
            break
        payload = buffer[cursor : cursor + length]
        if masked:
            payload = bytes(byte ^ mask_key[i % 4] for i, byte in enumerate(payload))
        frames.append((opcode, payload))
        offset = cursor + length
    return frames, buffer[offset:]


@dataclass
class Handshake:
    status_line: str
    headers: dict[str, str]
    accept_valid: bool
    leftover_bytes: int


class NativeWebSocketClient:
    """One bounded WebSocket connection to the app-server control socket."""

    def __init__(self, socket_path: Path | str) -> None:
        self.socket_path = str(socket_path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(HANDSHAKE_TIMEOUT)
        self.sock.connect(self.socket_path)
        request, key = build_client_handshake()
        self.sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                msg = f"app-server closed during upgrade handshake: {response!r}"
                raise NativeProtocolError(msg)
            response += chunk
        head, _, leftover = response.partition(b"\r\n\r\n")
        lines = head.decode("utf-8", errors="replace").split("\r\n")
        headers = {
            line.split(":", 1)[0].strip().lower(): line.split(":", 1)[1].strip()
            for line in lines[1:]
            if ":" in line
        }
        self.handshake = Handshake(
            status_line=lines[0],
            headers=headers,
            accept_valid=headers.get("sec-websocket-accept") == expected_accept(key),
            leftover_bytes=len(leftover),
        )
        if "101" not in self.handshake.status_line:
            msg = f"upgrade refused: {self.handshake.status_line}"
            raise NativeProtocolError(msg)
        self._buffer = bytearray(leftover)
        self._pending: list[tuple[int, bytes]] = []
        self._next_id = 0
        self.notifications: list[dict] = []
        self.server_requests: list[dict] = []
        self.overflowed = False

    # -- framing ---------------------------------------------------------

    def _send_frame(self, payload: bytes, opcode: int = OP_TEXT) -> None:
        self.sock.sendall(encode_frame(payload, opcode, mask=True))

    def _recv_frame(self, timeout: float) -> tuple[int, bytes]:
        """Read exactly one frame, answering pings; raises on close/timeout."""
        deadline = time.monotonic() + timeout
        while True:
            if self._pending:
                opcode, payload = self._pending.pop(0)
                if opcode == OP_PING:
                    self._send_frame(payload, OP_PONG)
                    continue
                if opcode == OP_PONG:
                    continue
                if opcode == OP_CLOSE:
                    msg = "app-server sent a WebSocket close frame"
                    raise NativeProtocolError(msg)
                return opcode, payload
            frames, remainder = decode_frames(bytes(self._buffer))
            self._buffer = bytearray(remainder)
            self._pending.extend(frames)
            if self._pending:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"timed out after {timeout}s waiting for a frame"
                raise TimeoutError(msg)
            self.sock.settimeout(max(remaining, 0.05))
            try:
                chunk = self.sock.recv(65536)
            except TimeoutError:
                continue  # the frame deadline, not the socket, decides
            if not chunk:
                msg = "app-server closed the connection"
                raise NativeProtocolError(msg)
            self._buffer += chunk

    def _send_json(self, message: dict) -> None:
        self._send_frame(json.dumps(message).encode())

    def _stash(self, message: dict) -> None:
        if message.get("method") is not None and message.get("id") is not None:
            if len(self.server_requests) >= MAX_BUFFERED_MESSAGES:
                self.overflowed = True
            self.server_requests.append(message)
        else:
            if len(self.notifications) >= MAX_BUFFERED_MESSAGES:
                self.overflowed = True
            self.notifications.append(message)

    # -- protocol --------------------------------------------------------

    def notify(self, method: str) -> None:
        self._send_json({"method": method})

    def request(self, method: str, params: dict | None = None, *, timeout: float = 60.0) -> dict:
        """Send a request and return its response (result or error, unjudged)."""
        self._next_id += 1
        request_id = self._next_id
        message: dict = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send_json(message)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"timed out after {timeout}s waiting for response to {method}"
                raise TimeoutError(msg)
            try:
                opcode, payload = self._recv_frame(min(remaining, 1.0))
            except TimeoutError:
                continue
            if opcode not in (OP_TEXT, OP_BINARY):
                continue
            incoming = json.loads(payload)
            if incoming.get("id") == request_id and ("result" in incoming or "error" in incoming):
                return incoming
            self._stash(incoming)

    def initialize(self, *, experimental: bool = False, name: str = "theater-native-proof") -> dict:
        params: dict = {"clientInfo": {"name": name, "title": name, "version": "0.1"}}
        if experimental:
            params["capabilities"] = {"experimentalApi": True}
        response = self.request("initialize", params)
        self.notify("initialized")
        return response

    def respond(self, server_request: dict, result: dict) -> None:
        """Answer a server->client request (approvals, server tools)."""
        self._send_json({"id": server_request["id"], "result": result})

    def wait_notification(self, method: str, *, timeout: float) -> dict:
        """Return the next notification named ``method``; buffer everything else."""
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self.notifications):
                if message.get("method") == method:
                    return self.notifications.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"timed out after {timeout}s waiting for notification {method}"
                raise TimeoutError(msg)
            try:
                opcode, payload = self._recv_frame(min(remaining, 1.0))
            except TimeoutError:
                continue
            if opcode not in (OP_TEXT, OP_BINARY):
                continue
            incoming = json.loads(payload)
            if incoming.get("method") == method:
                return incoming
            self._stash(incoming)

    def wait_server_request(self, method: str, *, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            for index, message in enumerate(self.server_requests):
                if message.get("method") == method:
                    return self.server_requests.pop(index)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                msg = f"timed out after {timeout}s waiting for server request {method}"
                raise TimeoutError(msg)
            try:
                opcode, payload = self._recv_frame(min(remaining, 1.0))
            except TimeoutError:
                continue
            if opcode not in (OP_TEXT, OP_BINARY):
                continue
            incoming = json.loads(payload)
            if incoming.get("method") == method and incoming.get("id") is not None:
                return incoming
            self._stash(incoming)

    def drain(self, *, quiet: float = 2.0) -> None:
        """Read until the socket stays quiet; used between test phases."""
        deadline = time.monotonic() + quiet
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                opcode, payload = self._recv_frame(min(remaining, 0.5))
            except TimeoutError:
                return
            if opcode in (OP_TEXT, OP_BINARY):
                self._stash(json.loads(payload))

    def close_abrupt(self) -> None:
        """Drop the connection without a WebSocket close frame (daemon-death)."""
        self.sock.close()

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._send_frame(b"", OP_CLOSE)
        self.sock.close()


@dataclass
class AppServerProcess:
    """A detached, test-owned `codex app-server --listen unix://<socket>`."""

    pid: int
    socket_path: Path
    log_path: Path
    codex_home: Path
    command: list[str] = field(default_factory=list)

    @classmethod
    def spawn(
        cls,
        *,
        codex_home: Path,
        socket_path: Path,
        log_path: Path,
        codex_binary: str = "codex",
        startup_timeout: float = 30.0,
        extra_env: dict[str, str] | None = None,
    ) -> AppServerProcess:
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        if extra_env:
            env.update(extra_env)
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        if socket_path.exists():
            socket_path.unlink()
        command = [codex_binary, "app-server", "--listen", f"unix://{socket_path}"]
        log_file = log_path.open("w")
        process = subprocess.Popen(
            command,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
        app_server = cls(
            pid=process.pid,
            socket_path=socket_path,
            log_path=log_path,
            codex_home=codex_home,
            command=command,
        )
        try:
            wait_until(
                socket_path.exists,
                timeout=startup_timeout,
                what="app-server control socket",
            )
            app_server.verify_identity()
        except Exception:
            app_server.terminate()
            raise
        return app_server

    def verify_identity(self) -> None:
        """Confirm the pid we hold is the codex app-server we launched."""
        os.kill(self.pid, 0)
        result = subprocess.run(
            ["ps", "-p", str(self.pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        if "codex" not in result.stdout or "app-server" not in result.stdout:
            msg = f"pid {self.pid} is not our codex app-server: {result.stdout!r}"
            raise NativeProtocolError(msg)

    def alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        return True

    def terminate(self) -> None:
        """Terminate the whole detached session, escalating to SIGKILL."""
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pid, signal.SIGTERM)
        if not wait_until(lambda: not self.alive(), timeout=10.0):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signal.SIGKILL)
        with contextlib.suppress(OSError):
            self.log_path.unlink(missing_ok=True)


class TmuxUi:
    """The native Codex TUI attached through an isolated tmux server."""

    COMPOSER_READY_MARK = "Ask Codex"

    def __init__(self, socket_path: Path) -> None:
        # `-S` keeps the server socket inside the test's own temporary root.
        # `-L <name>` would place it under the suite's session TMUX_TMPDIR,
        # where the conftest tmux guard would mistake it for an escaped server.
        self.socket_path = socket_path

    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        result = subprocess.run(
            ["tmux", "-S", str(self.socket_path), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            msg = f"tmux {' '.join(args)} failed: {result.stderr.strip()}"
            raise RuntimeError(msg)
        return result

    def launch(self, shell_command: str, *, width: int = 220, height: int = 50) -> None:
        self._run("kill-server")
        self._run("new-session", "-d", "-x", str(width), "-y", str(height), shell_command)

    def pane(self) -> str:
        return self._run("capture-pane", "-p", "-t", "0").stdout

    def session_alive(self) -> bool:
        """False once the launched process exits and the tmux server dies."""
        return self._run("list-sessions").returncode == 0

    def send_keys(self, keys: str) -> None:
        self._run("send-keys", "-t", "0", keys)

    def wait_ready(self, history_marker: str, *, timeout: float = 60.0) -> str:
        """State-based UI readiness: history rendered and composer visible."""
        content = ""
        ok = wait_until(
            lambda: (
                history_marker in (content := self.pane()) and self.COMPOSER_READY_MARK in content
            ),
            timeout=timeout,
        )
        if not ok:
            msg = f"native UI not ready after {timeout}s; pane tail: {content[-400:]!r}"
            raise TimeoutError(msg)
        return content

    def type_and_submit(self, text: str, *, timeout: float = 30.0) -> None:
        """Type into the composer, wait until it rendered, then press Enter."""
        self.send_keys(text)
        wait_until(lambda: text in self.pane(), timeout=timeout, what="typed text to render")
        self.send_keys("Enter")

    def kill(self) -> None:
        self._run("kill-server")


def write_isolated_codex_home(
    root: Path, *, trusted_paths: list[Path], reasoning_effort: str = "medium"
) -> Path:
    """Build an isolated CODEX_HOME reusing the ambient provider config.

    The ambient ``~/.codex/config.toml`` is copied with one change: the
    reasoning effort is lowered to a bounded level so proof turns complete
    within the smoke timeouts even when the ambient setting is ``max``. The
    provider selection (which may point at a custom provider; its contents are
    never logged or asserted on) is kept verbatim, and project trust entries
    for the test's temporary directories are appended. Credentials stay in the
    environment: only ``env_key`` references move.
    """
    codex_home = root / "home"
    codex_home.mkdir(parents=True)
    ambient = Path.home() / ".codex" / "config.toml"
    text = ambient.read_text() if ambient.exists() else ""
    # `model_reasoning_effort` is a top-level TOML key: replace an existing
    # value in place, or insert one before the first table header.
    effort_line = f'model_reasoning_effort = "{reasoning_effort}"'
    if re.search(r"(?m)^model_reasoning_effort\s*=", text):
        text = re.sub(r"(?m)^model_reasoning_effort\s*=.*$", effort_line, text, count=1)
    else:
        first_table = re.search(r"(?m)^\[", text)
        if first_table is None:
            text = f"{text}\n{effort_line}\n" if text.strip() else f"{effort_line}\n"
        else:
            text = f"{text[: first_table.start()]}{effort_line}\n\n{text[first_table.start() :]}"
    target = codex_home / "config.toml"
    target.write_text(text)
    with target.open("a") as handle:
        handle.write("\n# theater native proof isolation\n")
        for trusted in trusted_paths:
            handle.write(f'\n[projects."{trusted}"]\ntrust_level = "trusted"\n')
    return codex_home
