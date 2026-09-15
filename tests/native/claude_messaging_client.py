"""Test-only conformance harness for Claude Code's documented messaging socket.

Drives one disposable stock session through the Phase 0 gates of
docs/native-interaction/claude.md and records a redacted, machine-readable
schema-2 document. Wire facts are pinned to the official documentation plus
static inspection of the stock 2.1.248 binary; the live gates only ever run
against that real binary, and every required subcase must carry classified
evidence or its gate records an honest no-go. No production code may import
this module. FakeInbox here is a self-test receiver model, never evidence.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import uuid as uuid_module
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Official floors: same-machine messaging works on every provider from 2.1.248.
SAME_MACHINE_FLOOR = (2, 1, 248)
FEATURE_FLOOR = (2, 1, 224)
# Stock 2.1.248 receiver constants, verified by static binary inspection.
MAX_LINE_CHARS = 1_048_576
FIRST_LINE_DEADLINE_SECONDS = 30.0
SUN_PATH_LIMIT = 103
MSG_ID_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")
PRIORITIES = ("now", "next", "later")
RECEIPT_STATUSES = ("held", "denied", "expired", "delivered", "refused", "dropped")

# Provenance of the pinned wire constants above (static 2.1.248 inspection).
STATIC_INSPECTED_VERSION = "2.1.248"
STATIC_GIT_SHA = "8c9482ad0510ad5e3c88f0ebe6f035ec148f73e2"
STATIC_BUILD_TIME = "2026-08-27T19:29:54Z"

CONNECT_TIMEOUT = 5.0
REPLY_WAIT_SECONDS = 3.0
MAX_RECEIPTS = 16
MAX_TRANSCRIPT_RECORDS = 512
ROSTER_DIR = Path.home() / ".claude" / "sessions"
PROOF_ENV = "THEATER_CLAUDE_NATIVE_PROOF"
PROOF_BINARY_ENV = "THEATER_CLAUDE_NATIVE_PROOF_BINARY"
VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
PASS_RESULT_PREFIX = "pass: idle send only"
REQUIRED_GATE_NAMES = (
    "session_start",
    "auth",
    "idle_submission",
    "duplicate_msg_id",
    "admission_fact",
    "turn_mapping",
    "failure_taxonomy",
    "busy_behavior",
    "own_child_delivery",
    "session_rotation",
    "stale_credentials",
)
# Fixture-tree component that marks immutable reviewed evidence.
FIXTURE_TREE_COMPONENT = "fixtures"


class ConformanceError(RuntimeError):
    """A conformance step failed; messages never contain the messaging token."""


def _digest(path: str) -> tuple[str, int]:
    """SHA-256 and byte size of the exact executable the gates will launch."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


@dataclass(frozen=True)
class BinaryFacts:
    resolved: str
    version: tuple[int, int, int]
    sha256: str
    size_bytes: int

    @property
    def qualified(self) -> bool:
        return self.version >= SAME_MACHINE_FLOOR

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version)


def binary_facts(path: str | None = None) -> BinaryFacts | None:
    """Probe the exact stock binary; None means no readable release."""
    requested = path or os.environ.get(PROOF_BINARY_ENV) or "claude"
    resolved = shutil.which(requested) or requested
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = VERSION_PATTERN.search(result.stdout or "")
    if result.returncode != 0 or match is None:
        return None
    try:
        sha256, size = _digest(resolved)
    except OSError:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return BinaryFacts(
        resolved=os.path.realpath(resolved),
        version=(major, minor, patch),
        sha256=sha256,
        size_bytes=size,
    )


def auth_frame(token: str) -> dict:
    """The documented first-line authentication frame."""
    return {"type": "auth", "token": token}


def user_frame(
    content: str,
    *,
    msg_id: str,
    message_uuid: str | None = None,
    session_id: str | None = None,
    priority: str = "next",
    from_addr: str | None = None,
) -> dict:
    """Build the candidate user frame; unknown fields are never invented."""
    if not isinstance(content, str) or not content:
        raise ValueError("user frame content must be a non-empty string")
    if not MSG_ID_PATTERN.fullmatch(msg_id):
        raise ValueError("msg_id must match the stock receiver's bounded-id charset")
    if priority not in PRIORITIES:
        raise ValueError("priority must be one of the stock receiver's values")
    frame: dict[str, object] = {
        "type": "user",
        "message": {"role": "user", "content": content},
        "msg_id": msg_id,
    }
    if message_uuid is not None:
        frame["uuid"] = message_uuid
    if session_id is not None:
        frame["session_id"] = session_id
    if from_addr is not None:
        frame["from"] = from_addr
    if priority != "next":
        frame["priority"] = priority
    return frame


def redact(value: object, secrets: tuple[str, ...]) -> object:
    """Deep-copy with every secret substring replaced before fixture writes."""
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "<redacted>")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: redact(item, secrets) for key, item in value.items()}
    return value


def encode_line(frame: dict[str, object] | str) -> bytes:
    line = frame if isinstance(frame, str) else json.dumps(frame, separators=(",", ":"))
    if len(line) > MAX_LINE_CHARS:
        raise ConformanceError("frame exceeds the stock receiver's line cap")
    return (line + "\n").encode()


class MessagingClient:
    """One NDJSON client connection to a session's inbox socket."""

    def __init__(self, socket_path: str, *, token: str | None = None) -> None:
        self.socket_path = socket_path
        self._token = token
        self._sock: socket.socket | None = None

    def connect(self, *, timeout: float = CONNECT_TIMEOUT, auth_first: bool = True) -> None:
        if auth_first and not self._token:
            raise ConformanceError("authenticated connection requires the session token")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.socket_path)
        except OSError as exc:
            sock.close()
            raise ConformanceError(f"inbox socket refused connection: {exc}") from exc
        self._sock = sock
        if auth_first:
            self.send(auth_frame(self._token or ""))

    def send(self, frame: dict[str, object] | str) -> None:
        if self._sock is None:
            raise ConformanceError("client is not connected")
        try:
            self._sock.sendall(encode_line(frame))
        except OSError as exc:
            raise ConformanceError(f"inbox socket write failed: {exc}") from exc

    def send_raw(self, line: str) -> None:
        """Write one pre-shaped line, bypassing the line-cap guard."""
        if self._sock is None:
            raise ConformanceError("client is not connected")
        try:
            self._sock.sendall((line + "\n").encode())
        except OSError as exc:
            raise ConformanceError(f"inbox socket write failed: {exc}") from exc

    def alive(self) -> bool:
        """Peer-state probe that neither sends nor consumes inbox bytes."""
        if self._sock is None:
            return False
        previous = self._sock.gettimeout()
        self._sock.settimeout(0.5)
        try:
            data = self._sock.recv(1, socket.MSG_PEEK)
        except (BlockingIOError, TimeoutError):
            return True
        except OSError:
            return False
        finally:
            self._sock.settimeout(previous)
        return bool(data)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> MessagingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class ReplyListener:
    """Reply socket bound in the inbox's own vetted directory (receiver rule)."""

    def __init__(self, inbox_path: str) -> None:
        parent = Path(inbox_path).parent
        name = f"r-{uuid_module.uuid4().hex[:10]}.sock"
        self.path = str(parent / name)
        if len(self.path.encode()) >= SUN_PATH_LIMIT:
            raise ConformanceError("reply socket path exceeds the sun_path ceiling")
        self._server: socket.socket | None = None

    def start(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(4)
        self._server = server

    @property
    def address(self) -> str:
        return f"uds:{self.path}"

    def receipts(self, *, timeout: float = REPLY_WAIT_SECONDS) -> list[dict]:
        """Collect bounded receipt frames until the deadline; never blocks later."""
        if self._server is None:
            raise ConformanceError("reply listener is not started")
        deadline = time.monotonic() + timeout
        frames: list[dict] = []
        while len(frames) < MAX_RECEIPTS and time.monotonic() < deadline:
            remaining = max(deadline - time.monotonic(), 0.01)
            self._server.settimeout(remaining)
            try:
                conn, _ = self._server.accept()
            except OSError:
                break
            with conn:
                conn.settimeout(remaining)
                data = b""
                try:
                    while b"\n" not in data and len(data) <= MAX_LINE_CHARS:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
            for line in data.decode(errors="replace").splitlines():
                try:
                    parsed = json.loads(line)
                except ValueError:
                    continue
                if isinstance(parsed, dict):
                    frames.append(parsed)
        return frames

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            finally:
                self._server = None
        with contextlib.suppress(OSError):
            Path(self.path).unlink()


@dataclass
class _ConnState:
    authed: str | None = None
    saw_line: bool = False


class FakeInbox:
    """Self-test receiver model of the verified 2.1.248 verdict subset.

    Only the harness's own self-tests use this; live gates always run against
    the real stock binary, and no fixture may treat FakeInbox success as
    stock evidence.
    """

    def __init__(
        self,
        path: str,
        *,
        token: str,
        session_id: str,
        require_auth: bool = False,
    ) -> None:
        self.path = path
        self._token = token
        self._session_id = session_id
        self._require_auth = require_auth
        self._server: socket.socket | None = None
        self.events: list[str] = []
        self.frames: list[dict] = []

    def start(self) -> None:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.path)
        server.listen(4)
        self._server = server

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.close()
            finally:
                self._server = None
        with contextlib.suppress(OSError):
            Path(self.path).unlink()

    def serve_once(self, *, timeout: float = 5.0) -> None:
        """Accept exactly one connection and apply the receiver's verdicts."""
        if self._server is None:
            raise ConformanceError("fake inbox is not started")
        self._server.settimeout(timeout)
        try:
            conn, _ = self._server.accept()
        except OSError as exc:
            raise ConformanceError(f"fake inbox saw no connection: {exc}") from exc
        with conn:
            conn.settimeout(timeout)
            self._serve_connection(conn)

    def _serve_connection(self, conn: socket.socket) -> None:
        buffer = b""
        state = _ConnState()
        while True:
            while b"\n" not in buffer:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    # Read deadline or peer reset: nothing more to apply.
                    return
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_LINE_CHARS:
                    self.events.append("dropped-oversized-line")
                    return
            raw, buffer = buffer.split(b"\n", 1)
            if not self._apply_line(raw.decode(errors="replace"), state):
                return

    def _apply_line(self, line: str, state: _ConnState) -> bool:
        """Apply one inbox line; False closes the connection."""
        if not line.strip():
            if self._require_auth and state.authed is None and not state.saw_line:
                self.events.append("dropped-blank-before-auth")
                return False
            return True
        state.saw_line = True
        try:
            frame = json.loads(line)
        except ValueError:
            return self._reject_unparseable(state)
        if not isinstance(frame, dict) or not isinstance(frame.get("type"), str):
            self.events.append("warn-no-type")
            return True
        if frame["type"] == "auth":
            return self._apply_auth(frame, state)
        if self._require_auth and state.authed is None:
            self.events.append("dropped-unauthenticated-frame")
            return False
        self._handle(frame)
        return True

    def _reject_unparseable(self, state: _ConnState) -> bool:
        if self._require_auth and state.authed is None:
            self.events.append("dropped-unparseable-before-auth")
            return False
        self.events.append("warn-unparseable-line")
        return True

    def _apply_auth(self, frame: dict, state: _ConnState) -> bool:
        if frame.get("token") == self._token:
            state.authed = "child"
            self.events.append("auth-ok")
            return True
        self.events.append("auth-bad")
        return not self._require_auth

    def _handle(self, frame: dict) -> None:
        self.frames.append(frame)
        if frame["type"] != "user":
            self.events.append(f"control-{frame.get('action')}")
            return
        session_id = frame.get("session_id")
        if session_id is not None and session_id != self._session_id:
            self.events.append("dropped-session-id-mismatch")
            return
        message = frame.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content:
            self.events.append("ignored-empty-content")
            return
        self.events.append("held")
        self._receipt(frame, "held")

    def _receipt(self, frame: dict, status: str) -> None:
        reply = frame.get("from")
        if not isinstance(reply, str) or not reply.startswith("uds:"):
            self.events.append("receipt-skipped-unshaped-reply")
            return
        receipt: dict[str, object] = {
            "type": "control",
            "action": "peer_message_status",
            "status": status,
        }
        msg_id = frame.get("msg_id")
        if isinstance(msg_id, str) and MSG_ID_PATTERN.fullmatch(msg_id):
            receipt["orig_msg_id"] = msg_id
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            try:
                sock.connect(reply.removeprefix("uds:"))
                sock.sendall((json.dumps(receipt) + "\n").encode())
            except OSError:
                self.events.append("receipt-send-failed")


@dataclass(frozen=True)
class SessionFacts:
    session_id: str
    socket_path: str
    transcript_path: Path
    pid: int


_OWN_CHILD_POST_CODE = (
    "import json,os,socket,sys\n"
    "marker = sys.argv[1]\n"
    "s = socket.socket(socket.AF_UNIX)\n"
    "s.connect(os.environ['CLAUDE_CODE_MESSAGING_SOCKET'])\n"
    "auth = json.dumps({'type': 'auth', 'token': "
    "os.environ['CLAUDE_CODE_MESSAGING_TOKEN']})\n"
    "s.sendall((auth + chr(10)).encode())\n"
    "frame = json.dumps({'type': 'user', 'message': "
    "{'role': 'user', 'content': marker}})\n"
    "s.sendall((frame + chr(10)).encode())\n"
    "s.close()\n"
)


def _capture_hook_command(capture: Path, own_child_marker: str | None) -> str:
    """SessionStart hook: persist env+payload, then optionally self-post a frame."""
    pending = capture.with_name(f".{capture.name}.pending")
    capture_part = (
        "umask 077; { printf 'SOCKET=%s\\nTOKEN=%s\\n' "
        '"$CLAUDE_CODE_MESSAGING_SOCKET" "$CLAUDE_CODE_MESSAGING_TOKEN"; cat; } > '
        + shlex.quote(str(pending))
        + " && mv "
        + shlex.quote(str(pending))
        + " "
        + shlex.quote(str(capture))
    )
    if own_child_marker is None:
        return capture_part
    # The hook is the session's own child, so this post exercises the
    # documented own-child delivery path while the hook process is alive.
    post = "python3 -c " + shlex.quote(_OWN_CHILD_POST_CODE) + " " + shlex.quote(own_child_marker)
    return capture_part + "; " + post


def verify_capture_artifact(capture: Path) -> None:
    """The token-bearing capture must be a private regular file in a 0700 dir."""
    if capture.is_symlink():
        raise ConformanceError("SessionStart capture must not be a symlink")
    parent_mode = stat.S_IMODE(capture.parent.stat().st_mode)
    if parent_mode != 0o700:
        raise ConformanceError("SessionStart capture directory must hold mode 0700")
    mode = stat.S_IMODE(capture.stat().st_mode)
    if mode != 0o600:
        raise ConformanceError("SessionStart capture must hold mode 0600")


def _roster_pid(session_id: str) -> int | None:
    """Find the roster pid file registered for exactly this session id."""
    try:
        entries = sorted(ROSTER_DIR.glob("*.json"))
    except OSError:
        return None
    for entry in entries:
        try:
            data = json.loads(entry.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("sessionId") == session_id:
            name = entry.stem
            if name.isdigit():
                return int(name)
    return None


def _socket_connects(path: str, *, timeout: float = 1.0) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout)
            probe.connect(path)
    except OSError:
        return False
    return True


def _launch_argv(
    binary: str,
    *,
    settings_path: Path,
    session_id: str,
    resume: str | None,
) -> list[str]:
    argv = [binary, f"--settings={settings_path}", "--dangerously-skip-permissions"]
    argv.append(f"--resume={resume}" if resume else f"--session-id={session_id}")
    return argv


class ProofSession:
    """One disposable stock interactive session under tmux with a capture hook."""

    def __init__(self, binary: str, *, base_dir: Path) -> None:
        self.binary = binary
        self.base_dir = base_dir
        self.session_id = str(uuid_module.uuid4())
        self.facts: SessionFacts | None = None
        self._token: str | None = None
        self._tmux_name: str | None = None
        self._capture: Path | None = None
        self._tmux_socket = base_dir / "tmux.sock"

    def _tmux_argv(self, *args: str) -> list[str]:
        return ["tmux", "-S", str(self._tmux_socket), *args]

    def start(
        self,
        *,
        settings_extra: dict | None = None,
        own_child_marker: str | None = None,
        resume: str | None = None,
        timeout: float = 120.0,
    ) -> SessionFacts:
        """Launch under tmux and wait for the SessionStart hook capture."""
        runtime_dir = self.base_dir / "runtime"
        runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        capture = self.base_dir / f"hook-{self.session_id}.json"
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": _capture_hook_command(capture, own_child_marker),
                                "async": True,
                            }
                        ]
                    }
                ]
            },
            **(settings_extra or {}),
        }
        settings_path = self.base_dir / f"settings-{self.session_id}.json"
        settings_path.write_text(json.dumps(settings) + "\n")
        argv = _launch_argv(
            self.binary,
            settings_path=settings_path,
            session_id=self.session_id,
            resume=resume,
        )
        self._tmux_name = f"theater-claude-proof-{self.session_id[:8]}"
        self._capture = capture
        launch = [
            "tmux",
            "-f",
            "/dev/null",
            "-S",
            str(self._tmux_socket),
            "new-session",
            "-d",
            "-s",
            self._tmux_name,
            "-x",
            "220",
            "-y",
            "50",
            "env",
            f"XDG_RUNTIME_DIR={runtime_dir}",
            *argv,
        ]
        completed = subprocess.run(launch, capture_output=True, text=True, check=False)
        if completed.returncode != 0:
            raise ConformanceError(f"tmux launch failed: {completed.stderr.strip()}")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if capture.exists():
                break
            if not self._tmux_alive():
                raise ConformanceError("proof session exited before SessionStart capture")
            time.sleep(0.2)
        else:
            raise ConformanceError("SessionStart capture did not land before the deadline")
        try:
            verify_capture_artifact(capture)
            facts = self._parse_capture(capture)
        finally:
            with contextlib.suppress(OSError):
                capture.unlink()
            self._capture = None
        self.facts = facts
        return facts

    def resume(self, session_id: str) -> SessionFacts:
        """Relaunch the same session id under --resume with a fresh capture."""
        self.facts = None
        self._token = None
        return self.start(resume=session_id)

    def _parse_capture(self, capture: Path) -> SessionFacts:
        text = capture.read_text()
        lines = text.splitlines()
        socket_path = None
        token = None
        for line in lines:
            if line.startswith("SOCKET="):
                socket_path = line.removeprefix("SOCKET=")
            elif line.startswith("TOKEN="):
                token = line.removeprefix("TOKEN=")
        body = "\n".join(lines[2:])
        try:
            loaded = json.loads(body)
        except ValueError as exc:
            raise ConformanceError("SessionStart hook payload is not valid JSON") from exc
        payload = loaded if isinstance(loaded, dict) else {}
        if not socket_path or not token:
            raise ConformanceError("SessionStart hook did not expose socket and token")
        session_id = payload.get("session_id")
        transcript = payload.get("transcript_path")
        if not isinstance(session_id, str) or not isinstance(transcript, str):
            raise ConformanceError("SessionStart hook payload lacks identity fields")
        pid = _roster_pid(session_id)
        if pid is None:
            raise ConformanceError("session roster did not register the proof session")
        self._token = token
        return SessionFacts(
            session_id=session_id,
            socket_path=socket_path,
            transcript_path=Path(transcript),
            pid=pid,
        )

    @property
    def socket_path(self) -> str:
        if self.facts is None:
            raise ConformanceError("proof session has not started")
        return self.facts.socket_path

    @property
    def live_session_id(self) -> str:
        if self.facts is None:
            raise ConformanceError("proof session has not started")
        return self.facts.session_id

    @property
    def token(self) -> str:
        if not self._token:
            raise ConformanceError("session token was not captured")
        return self._token

    @property
    def token_present(self) -> bool:
        return bool(self._token)

    def _tmux_alive(self) -> bool:
        if not self._tmux_name:
            return False
        completed = subprocess.run(
            self._tmux_argv("has-session", "-t", self._tmux_name),
            capture_output=True,
            check=False,
        )
        return completed.returncode == 0

    def screen_text(self) -> str:
        if not self._tmux_name:
            return ""
        completed = subprocess.run(
            self._tmux_argv("capture-pane", "-p", "-J", "-S", "-2000", "-t", self._tmux_name),
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.stdout if completed.returncode == 0 else ""

    def wait_for_record(self, predicate: Callable[[dict], bool], *, timeout: float = 90.0) -> dict:
        """Poll the bounded transcript until one record satisfies predicate."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for record in self.transcript_records():
                if predicate(record):
                    return record
            time.sleep(0.5)
        raise ConformanceError("transcript never satisfied the awaited record")

    def transcript_records(self) -> list[dict]:
        if self.facts is None:
            return []
        try:
            raw = self.facts.transcript_path.read_text(errors="replace")
        except OSError:
            return []
        records: list[dict] = []
        for line in raw.splitlines()[-MAX_TRANSCRIPT_RECORDS:]:
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                records.append(parsed)
        return records

    def stop(self, *, remove_transcript: bool = True) -> None:
        """Tear down tmux and every token-bearing artifact on any path."""
        if self._tmux_name and self._tmux_alive():
            subprocess.run(
                self._tmux_argv("kill-session", "-t", self._tmux_name),
                capture_output=True,
                check=False,
            )
        if self._capture is not None:
            for path in (
                self._capture,
                self._capture.with_name(f".{self._capture.name}.pending"),
            ):
                with contextlib.suppress(OSError):
                    path.unlink()
        if remove_transcript and self.facts is not None:
            with contextlib.suppress(OSError):
                self.facts.transcript_path.unlink()

    def socket_alive(self) -> bool:
        if self.facts is None:
            return False
        return _socket_connects(self.facts.socket_path)


@dataclass
class GateResult:
    name: str
    status: str
    evidence: dict[str, object] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass(frozen=True)
class SubcaseResult:
    """One required subcase: classified evidence, plus whether it is acceptable."""

    classification: str
    classified: bool
    ok: bool
    detail: object = None


def _gate_from_subcases(name: str, subcases: dict[str, SubcaseResult]) -> GateResult:
    """A gate passes only when every required subcase is classified and ok."""
    evidence: dict[str, object] = {
        key: {"classification": value.classification, "detail": value.detail}
        for key, value in subcases.items()
    }
    unclassified = sorted(key for key, value in subcases.items() if not value.classified)
    if unclassified:
        evidence["unclassified"] = unclassified
        return GateResult(name, "no-go", evidence)
    broken = sorted(key for key, value in subcases.items() if not value.ok)
    evidence["failing"] = broken
    return GateResult(name, "fail" if broken else "pass", evidence)


def _poll_until(predicate: Callable[[], bool], *, timeout: float, interval: float = 0.5) -> bool:
    """Deadline-bounded predicate polling; never a blind sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _wait_for_marker(session: ProofSession, marker: str, expected: int, *, timeout: float) -> bool:
    return _poll_until(lambda: count_marker(session, marker) >= expected, timeout=timeout)


def _marker_absent_through(session: ProofSession, marker: str, *, timeout: float) -> bool:
    """Bounded window in which the marker must never appear even once."""
    return not _poll_until(lambda: count_marker(session, marker) > 0, timeout=timeout)


def _alive_through(client: MessagingClient, *, seconds: float) -> bool:
    """True when the peer keeps the connection alive through the whole window."""
    return not _poll_until(lambda: not client.alive(), timeout=seconds, interval=0.25)


def _markers() -> tuple[str, str]:
    """One message id and one message uuid for a single proof operation."""
    return str(uuid_module.uuid4()), str(uuid_module.uuid4())


def _record_text(record: dict) -> str:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return " ".join(parts)
    return ""


def _find_marker(records: list[dict], marker: str) -> dict | None:
    for record in records:
        if marker in _record_text(record):
            return record
    return None


def count_marker(session: ProofSession, marker: str) -> int:
    """Bounded count of transcript user records carrying one marker."""
    return len([r for r in session.transcript_records() if marker in _record_text(r)])


def _send_and_listen(
    session: ProofSession,
    marker: str,
    *,
    token: str | None = None,
    auth_first: bool = True,
    session_id: str | None = None,
) -> tuple[list[dict], str | None]:
    """Send one marker frame with a reply socket; returns receipts and error."""
    listener = ReplyListener(session.socket_path)
    listener.start()
    error: str | None = None
    try:
        client = MessagingClient(session.socket_path, token=token)
        client.connect(auth_first=auth_first)
        client.send(
            user_frame(
                f"Reply with only the word: ok. (proof marker {marker})",
                msg_id=str(uuid_module.uuid4()),
                session_id=session.live_session_id if session_id is None else session_id,
                from_addr=listener.address,
            )
        )
        client.close()
        receipts = listener.receipts()
    except (ConformanceError, ValueError) as exc:
        receipts = []
        error = str(exc)
    finally:
        listener.stop()
    return receipts, error


def gate_session_start(session: ProofSession) -> GateResult:
    """Gate 1: SessionStart exposes a usable socket and token before any control."""
    facts = session.facts
    if facts is None:
        return GateResult("session_start", "fail", {"reason": "no captured facts"})
    init = next((r for r in session.transcript_records() if r.get("subtype") == "init"), None)
    evidence: dict[str, object] = {
        "socketPath": facts.socket_path,
        "socketBoundBeforeControl": session.socket_alive(),
        "tokenPresent": session.token_present,
        "rosterRegistered": facts.pid > 0,
        "initRecordsSocketPath": isinstance(init, dict)
        and init.get("messaging_socket_path") == facts.socket_path,
        "captureModeVerified": True,
    }
    ok = all(
        (
            evidence["socketBoundBeforeControl"],
            evidence["tokenPresent"],
            evidence["rosterRegistered"],
        )
    )
    return GateResult("session_start", "pass" if ok else "fail", evidence)


def _auth_valid_accepted(session: ProofSession) -> SubcaseResult:
    """Valid auth accepts the connection; a blank line must not drop it."""
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send("")
        survived = _alive_through(client, seconds=3.0)
        client.close()
    except ConformanceError as exc:
        return SubcaseResult(f"error: {exc}", classified=False, ok=False)
    return SubcaseResult(
        "survived-blank-line-after-auth" if survived else "connection-dropped-after-auth",
        classified=True,
        ok=survived,
    )


def _auth_rejected_credentials(unverified: ProofSession) -> SubcaseResult:
    """Rejected credentials must never be admitted, including as held."""
    marker = f"theater-proof-badauth-{uuid_module.uuid4().hex[:8]}"
    receipts, error = _send_and_listen(
        unverified, marker, token="not-a-child-token", auth_first=True
    )
    if error is not None:
        return SubcaseResult(f"error: {error}", classified=False, ok=False, detail=receipts)
    delivered = not _marker_absent_through(unverified, marker, timeout=6.0)
    held = any(r.get("status") == "held" for r in receipts)
    if delivered:
        return SubcaseResult(
            "bad-token-frame-delivered", classified=True, ok=False, detail=receipts
        )
    if held:
        return SubcaseResult("bad-token-frame-held", classified=True, ok=False, detail=receipts)
    return SubcaseResult(
        "bad-token-frame-silently-ignored",
        classified=True,
        ok=True,
        detail={"receipts": receipts, "absenceWindowSeconds": 6.0},
    )


def _auth_frame_before_auth(unverified: ProofSession) -> SubcaseResult:
    """No non-auth frame is accepted as the first frame of a connection."""
    marker = f"theater-proof-noauth-{uuid_module.uuid4().hex[:8]}"
    receipts, error = _send_and_listen(unverified, marker, token=None, auth_first=False)
    if error is not None:
        return SubcaseResult(f"error: {error}", classified=False, ok=False, detail=receipts)
    delivered = not _marker_absent_through(unverified, marker, timeout=6.0)
    held = any(r.get("status") == "held" for r in receipts)
    if delivered:
        return SubcaseResult("pre-auth-frame-delivered", classified=True, ok=False, detail=receipts)
    if held:
        return SubcaseResult("pre-auth-frame-held", classified=True, ok=False, detail=receipts)
    return SubcaseResult(
        "pre-auth-frame-silently-ignored",
        classified=True,
        ok=True,
        detail={"receipts": receipts, "absenceWindowSeconds": 6.0},
    )


def gate_auth(session: ProofSession, unverified: ProofSession) -> GateResult:
    """Gate 2: valid auth accepted, bad credentials fail, no pre-auth frame."""
    return _gate_from_subcases(
        "auth",
        {
            "validAuthAccepted": _auth_valid_accepted(session),
            "rejectedCredentials": _auth_rejected_credentials(unverified),
            "noFrameAcceptedBeforeAuth": _auth_frame_before_auth(unverified),
        },
    )


@dataclass(frozen=True)
class IdleSendResult:
    marker: str
    msg_id: str
    frame_uuid: str
    receipts: list[dict]
    user_record: dict


def _idle_send(
    session: ProofSession, marker: str, *, msg_id: str, message_uuid: str
) -> IdleSendResult:
    listener = ReplyListener(session.socket_path)
    listener.start()
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send(
            user_frame(
                f"Reply with only the word: ok. (proof marker {marker})",
                msg_id=msg_id,
                message_uuid=message_uuid,
                session_id=session.live_session_id,
                from_addr=listener.address,
            )
        )
        client.close()
        receipts = listener.receipts()
    finally:
        listener.stop()
    user_record = session.wait_for_record(
        lambda r: r.get("type") == "user" and marker in _record_text(r)
    )
    return IdleSendResult(
        marker=marker,
        msg_id=msg_id,
        frame_uuid=message_uuid,
        receipts=receipts,
        user_record=user_record,
    )


def gate_idle_submission(session: ProofSession) -> IdleSendResult:
    """Gate 3: one frame while idle submits exactly one user input and a turn."""
    msg_id, message_uuid = _markers()
    marker = f"theater-proof-idle-{msg_id[:8]}"
    result = _idle_send(session, marker, msg_id=msg_id, message_uuid=message_uuid)
    session.wait_for_record(lambda r: r.get("type") == "assistant" and bool(_record_text(r)))
    return result


def gate_duplicate(session: ProofSession, idle: IdleSendResult) -> dict[str, object]:
    """Gate 4: resend the exact frame; record upstream duplicate semantics."""
    before = count_marker(session, idle.marker)
    listener = ReplyListener(session.socket_path)
    listener.start()
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send(
            user_frame(
                f"Reply with only the word: ok. (proof marker {idle.marker})",
                msg_id=idle.msg_id,
                message_uuid=idle.frame_uuid,
                session_id=session.live_session_id,
                from_addr=listener.address,
            )
        )
        client.close()
        receipts = listener.receipts()
    finally:
        listener.stop()
    _wait_for_marker(session, idle.marker, before + 1, timeout=12.0)
    after = count_marker(session, idle.marker)
    semantics = _duplicate_semantics(before, after)
    return {"duplicateSemantics": semantics, "receipts": receipts}


def _duplicate_semantics(before: int, after: int) -> str:
    if after == before:
        return "duplicate-suppressed"
    if after == before + 1:
        return "duplicate-created-second-input"
    return f"unclassified-{after - before}"


def gate_admission_and_turn_mapping(
    session: ProofSession, idle: IdleSendResult
) -> tuple[dict[str, object], dict[str, object]]:
    """Gates 5+6: durable admission fact and the msg_id/uuid to transcript join."""
    user_record = idle.user_record
    record_uuid = user_record.get("uuid")
    transcript_blob = json.dumps(session.transcript_records())
    mapping: dict[str, object] = {
        "frameUuidEqualsRecordUuid": record_uuid == idle.frame_uuid,
        "msgIdInRecord": idle.msg_id in transcript_blob,
        "promptOrTurnIdEqualsFrameUuid": idle.frame_uuid
        in {user_record.get("promptId"), user_record.get("turnId")},
    }
    admission: dict[str, object] = {
        "socketReceiptOnAccept": bool(idle.receipts),
        "transcriptUserRecordIsAdmission": isinstance(record_uuid, str),
    }
    return admission, mapping


def _tax_malformed(session: ProofSession) -> SubcaseResult:
    """Malformed line: the receiver warns and keeps the connection."""
    marker = f"theater-proof-malformed-{uuid_module.uuid4().hex[:8]}"
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send('{"type": "user", "message": ')
        client.send(
            user_frame(
                f"Reply with only the word: ok. (proof marker {marker})",
                msg_id=str(uuid_module.uuid4()),
                session_id=session.live_session_id,
            )
        )
        survived = client.alive()
        client.close()
    except (ConformanceError, ValueError) as exc:
        return SubcaseResult(f"error: {exc}", classified=False, ok=False)
    delivered = _wait_for_marker(session, marker, 1, timeout=20.0)
    if survived and delivered:
        return SubcaseResult("connection-survived-frame-admitted", classified=True, ok=True)
    if not survived:
        return SubcaseResult("connection-dropped-on-malformed", classified=True, ok=False)
    return SubcaseResult(
        "connection-survived-frame-not-admitted",
        classified=True,
        ok=False,
        detail={"waitedSeconds": 20.0},
    )


def _tax_permission_refusal(unverified: ProofSession) -> SubcaseResult:
    """Probe the default permission rules; classify only an observed refusal."""
    marker = f"theater-proof-permission-{uuid_module.uuid4().hex[:8]}"
    receipts, error = _send_and_listen(unverified, marker, token=unverified.token)
    if error is not None:
        return SubcaseResult(f"error: {error}", classified=False, ok=False, detail=receipts)
    refused = [r for r in receipts if r.get("status") in ("refused", "expired")]
    if refused:
        return SubcaseResult("peer-refused-with-receipt", classified=True, ok=True, detail=receipts)
    held = [r for r in receipts if r.get("status") == "held"]
    if held:
        return SubcaseResult(
            "not-inducible: unverified peer held, no refusal observed",
            classified=False,
            ok=False,
            detail=receipts,
        )
    return SubcaseResult(
        "not-inducible: no refusal receipt observed",
        classified=False,
        ok=False,
        detail=receipts,
    )


def _receipt_suggests_rate_limit(receipt: dict) -> bool:
    """Match any receipt shape that names rate limiting; never guess beyond it."""
    blob = json.dumps(receipt).lower()
    return "rate" in blob or "limit" in blob


def _tax_rate_limit(session: ProofSession) -> SubcaseResult:
    """Bounded burst; classify only an observed rate-limit receipt."""
    marker_root = f"theater-proof-rate-{uuid_module.uuid4().hex[:8]}"
    listener = ReplyListener(session.socket_path)
    listener.start()
    receipts: list[dict] = []
    error: str | None = None
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        for index in range(12):
            client.send(
                user_frame(
                    f"Reply with only the word: ok. (proof marker {marker_root}-{index})",
                    msg_id=str(uuid_module.uuid4()),
                    session_id=session.live_session_id,
                    from_addr=listener.address,
                )
            )
        client.close()
        receipts = listener.receipts()
    except (ConformanceError, ValueError) as exc:
        error = str(exc)
    finally:
        listener.stop()
    if error is not None:
        return SubcaseResult(f"error: {error}", classified=False, ok=False, detail=receipts)
    healthy = _socket_connects(session.socket_path)
    if not healthy:
        return SubcaseResult("session-died-after-burst", classified=True, ok=False, detail=receipts)
    if any(_receipt_suggests_rate_limit(r) for r in receipts):
        return SubcaseResult("rate-limit-observed", classified=True, ok=True, detail=receipts)
    return SubcaseResult(
        "not-inducible: no rate-limit receipt at a 12-frame burst",
        classified=False,
        ok=False,
        detail=receipts,
    )


def _tax_disconnect_before_reply(session: ProofSession) -> SubcaseResult:
    """Client closes before any reply is read; the session must stay healthy."""
    marker = f"theater-proof-disconnect-{uuid_module.uuid4().hex[:8]}"
    listener = ReplyListener(session.socket_path)
    listener.start()
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send(
            user_frame(
                f"Reply with only the word: ok. (proof marker {marker})",
                msg_id=str(uuid_module.uuid4()),
                session_id=session.live_session_id,
                from_addr=listener.address,
            )
        )
        client.close()
    except (ConformanceError, ValueError) as exc:
        listener.stop()
        return SubcaseResult(f"error: {exc}", classified=False, ok=False)
    listener.stop()
    delivered = _wait_for_marker(session, marker, 1, timeout=20.0)
    healthy = _socket_connects(session.socket_path)
    if healthy and delivered:
        return SubcaseResult("session-survived-early-disconnect", classified=True, ok=True)
    if not healthy:
        return SubcaseResult("session-died-after-early-disconnect", classified=True, ok=False)
    return SubcaseResult(
        "frame-lost-after-early-disconnect",
        classified=True,
        ok=False,
        detail={"waitedSeconds": 20.0},
    )


_EXIT_PROBE_CODE = (
    "import json,os,socket,sys\n"
    "s = socket.socket(socket.AF_UNIX)\n"
    "s.connect(os.environ['THEATER_CLAUDE_PROOF_SOCKET'])\n"
    "auth = json.dumps({'type': 'auth', "
    "'token': os.environ['THEATER_CLAUDE_PROOF_TOKEN']})\n"
    "s.sendall((auth + chr(10)).encode())\n"
    "frame = json.dumps({'type': 'user', 'message': "
    "{'role': 'user', 'content': sys.argv[1]}})\n"
    "s.sendall((frame + chr(10)).encode())\n"
    "s.close()\n"
)


def _tax_immediate_process_exit(session: ProofSession) -> SubcaseResult:
    """A client process exits immediately after writing; classify the outcome."""
    marker = f"theater-proof-exit-{uuid_module.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["python3", "-c", _EXIT_PROBE_CODE, marker],
            env={
                **os.environ,
                "THEATER_CLAUDE_PROOF_SOCKET": session.socket_path,
                "THEATER_CLAUDE_PROOF_TOKEN": session.token,
            },
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SubcaseResult(f"error: {exc}", classified=False, ok=False)
    delivered = _wait_for_marker(session, marker, 1, timeout=20.0)
    healthy = _socket_connects(session.socket_path)
    if healthy and delivered:
        return SubcaseResult("session-survived-client-exit", classified=True, ok=True)
    if not healthy:
        return SubcaseResult("session-died-after-client-exit", classified=True, ok=False)
    return SubcaseResult(
        "frame-lost-after-client-exit",
        classified=True,
        ok=False,
        detail={"waitedSeconds": 20.0},
    )


def _taxonomy_empty_content(session: ProofSession) -> str:
    """Empty content must be ignored without dropping the connection."""
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send_raw(json.dumps({"type": "user", "message": {"role": "user", "content": ""}}))
        survived = _alive_through(client, seconds=3.0)
        client.close()
    except ConformanceError as exc:
        return f"error: {exc}"
    return "ignored-connection-survived" if survived else "connection-dropped"


def _taxonomy_session_mismatch(session: ProofSession) -> dict[str, object]:
    """Wrong session id is dropped; the marker never reaches the transcript."""
    marker = f"theater-proof-session-id-{uuid_module.uuid4().hex[:8]}"
    mismatched = f"{session.live_session_id}-mismatch"
    receipts, error = _send_and_listen(
        session,
        marker,
        token=session.token,
        session_id=mismatched,
    )
    if error is not None:
        return {"error": error}
    absent = _marker_absent_through(session, marker, timeout=8.0)
    return {"delivered": not absent, "receipts": receipts}


def _taxonomy_oversized(session: ProofSession) -> str:
    """Oversized line: the receiver destroys the connection at the line cap."""
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send_raw("x" * (MAX_LINE_CHARS + 1))
        dropped = _poll_until(lambda: not client.alive(), timeout=3.0, interval=0.25)
        client.close()
    except ConformanceError as exc:
        return f"error: {exc}"
    return "dropped-connection" if dropped else "survived-past-window"


def gate_failure_taxonomy(session: ProofSession, unverified: ProofSession) -> GateResult:
    """Gate 7: the plan's named outcomes must each carry classified evidence."""
    gate = _gate_from_subcases(
        "failure_taxonomy",
        {
            "permission_refusal": _tax_permission_refusal(unverified),
            "rate_limit": _tax_rate_limit(session),
            "malformed_frame": _tax_malformed(session),
            "disconnect_before_reply": _tax_disconnect_before_reply(session),
            "immediate_process_exit": _tax_immediate_process_exit(session),
        },
    )
    gate.evidence["additionalObservations"] = {
        "emptyContent": _taxonomy_empty_content(session),
        "sessionMismatch": _taxonomy_session_mismatch(session),
        "oversizedLine": _taxonomy_oversized(session),
    }
    return gate


def gate_busy(session: ProofSession) -> dict[str, object]:
    """Gate 8: classify a busy frame without exposing it as Theater send."""
    long_marker = f"theater-proof-busy-long-{uuid_module.uuid4().hex[:8]}"
    mid_marker = f"theater-proof-busy-mid-{uuid_module.uuid4().hex[:8]}"
    listener = ReplyListener(session.socket_path)
    listener.start()
    try:
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send(
            user_frame(
                "Use the Bash tool to run exactly `sleep 12`, then reply with only "
                f"done-{long_marker}. (proof marker {long_marker})",
                msg_id=str(uuid_module.uuid4()),
                session_id=session.live_session_id,
                from_addr=listener.address,
            )
        )
        client.close()
        session.wait_for_record(
            lambda r: r.get("type") == "user" and long_marker in _record_text(r)
        )
        session.wait_for_record(
            lambda r: (
                r.get("type") == "assistant"
                and any(
                    isinstance(block, dict)
                    and block.get("type") == "tool_use"
                    and block.get("name") == "Bash"
                    and "sleep 12" in json.dumps(block.get("input"))
                    for block in (
                        r.get("message", {}).get("content", [])
                        if isinstance(r.get("message"), dict)
                        else []
                    )
                )
            )
        )
        client = MessagingClient(session.socket_path, token=session.token)
        client.connect()
        client.send(
            user_frame(
                f"Reply with only the word: ok. (proof marker {mid_marker})",
                msg_id=str(uuid_module.uuid4()),
                session_id=session.live_session_id,
                from_addr=listener.address,
            )
        )
        client.close()
        receipts = listener.receipts()
    finally:
        listener.stop()
    turn_completed = _poll_until(
        lambda: (
            _busy_record_indices(session.transcript_records(), long_marker, mid_marker)[2]
            is not None
        ),
        timeout=90.0,
    )
    if turn_completed:
        _marker_absent_through(session, mid_marker, timeout=12.0)
    records = session.transcript_records()
    long_index, mid_index, final_index = _busy_record_indices(records, long_marker, mid_marker)
    marker_in_editor = mid_marker in session.screen_text()
    classification = _classify_busy_behavior(
        long_index=long_index,
        mid_index=mid_index,
        final_index=final_index,
        marker_in_editor=marker_in_editor,
    )
    return {
        "classification": classification,
        "markerInEditor": marker_in_editor,
        "receipts": receipts,
        "turnCompleted": turn_completed,
        "exposedAsTheaterSend": False,
    }


def _busy_record_indices(
    records: list[dict], long_marker: str, mid_marker: str
) -> tuple[int | None, int | None, int | None]:
    long_index = next((i for i, r in enumerate(records) if long_marker in _record_text(r)), None)
    mid_index = next((i for i, r in enumerate(records) if mid_marker in _record_text(r)), None)
    final_index = None
    if long_index is not None:
        final_index = next(
            (
                i
                for i in range(long_index + 1, len(records))
                if records[i].get("type") == "assistant"
                and isinstance(records[i].get("message"), dict)
                and records[i]["message"].get("stop_reason") not in (None, "tool_use")
            ),
            None,
        )
    return long_index, mid_index, final_index


def _classify_busy_behavior(
    *,
    long_index: int | None,
    mid_index: int | None,
    final_index: int | None,
    marker_in_editor: bool,
) -> str:
    if long_index is None or final_index is None:
        return "unclassified"
    if mid_index is not None and long_index < mid_index < final_index:
        return "consumed-between-tool-calls"
    if mid_index is not None and mid_index > final_index:
        return "submitted-after-turn"
    if mid_index is None and marker_in_editor:
        return "editor-buffered-not-submitted"
    return "unclassified"


def gate_own_child(session: ProofSession, marker: str) -> dict[str, object]:
    """Own-child admission: the hook-posted frame must start a real turn."""
    record = _find_marker(session.transcript_records(), marker)
    return {"hookPostedMarkerDelivered": record is not None}


def _rotation_old_paths_removed(session: ProofSession, stale_socket: str) -> SubcaseResult:
    """Exit must unlink the old socket and remove the roster entry."""
    pid = session.facts.pid if session.facts is not None else None
    session.stop(remove_transcript=False)
    if pid is None:
        return SubcaseResult("no-session-to-rotate", classified=False, ok=False)
    refused = _poll_until(lambda: not _socket_connects(stale_socket), timeout=8.0, interval=0.25)
    roster = ROSTER_DIR / f"{pid}.json"
    removed = _poll_until(lambda: not roster.exists(), timeout=8.0, interval=0.25)
    ok = refused and removed
    classification = (
        "old-socket-refused-and-roster-removed" if ok else "old-paths-lingered-after-exit"
    )
    return SubcaseResult(classification, classified=True, ok=ok)


def _rotation_resume_rebinds(session: ProofSession, old_socket: str) -> SubcaseResult:
    """Resume must bind a fresh socket and re-register the roster entry."""
    old_session_id = session.live_session_id
    session.resume(old_session_id)
    bound = _poll_until(session.socket_alive, timeout=15.0)
    registered = _roster_pid(session.live_session_id) is not None
    init = next(
        (
            r
            for r in session.transcript_records()
            if r.get("subtype") == "init" and r.get("messaging_socket_path") == session.socket_path
        ),
        None,
    )
    new_socket_differs = session.socket_path != old_socket
    details = {
        "oldSocket": old_socket,
        "newSocketDiffers": new_socket_differs,
        "rosterRegistered": registered,
        "initRecordsNewSocketPath": init is not None,
    }
    ok = bound and new_socket_differs and registered
    return SubcaseResult(
        "resume-rebound-fresh-socket" if ok else "resume-did-not-rebind",
        classified=True,
        ok=ok,
        detail=details,
    )


def _rotation_stale_token(session: ProofSession, stale_token: str) -> SubcaseResult:
    """The pre-rotation token must not mutate the resumed session."""
    marker = f"theater-proof-rotstale-{uuid_module.uuid4().hex[:8]}"
    receipts, error = _send_and_listen(session, marker, token=stale_token)
    if error is not None:
        return SubcaseResult(f"error: {error}", classified=False, ok=False, detail=receipts)
    delivered = not _marker_absent_through(session, marker, timeout=8.0)
    accepted = bool(receipts)
    if delivered or accepted:
        return SubcaseResult(
            "stale-token-accepted-by-resumed-session",
            classified=True,
            ok=False,
            detail={"delivered": delivered, "receipts": receipts},
        )
    return SubcaseResult(
        "stale-token-rejected-by-resumed-session",
        classified=True,
        ok=True,
        detail={"receipts": receipts, "absenceWindowSeconds": 8.0},
    )


def gate_rotation(session: ProofSession, *, stale_socket: str, stale_token: str) -> GateResult:
    """Gate 9: rotation cleans old paths, resume rebinds, stale credentials die."""
    return _gate_from_subcases(
        "session_rotation",
        {
            "oldPathsRemoved": _rotation_old_paths_removed(session, stale_socket),
            "resumeRebinds": _rotation_resume_rebinds(session, stale_socket),
            "staleTokenCannotMutateResumed": _rotation_stale_token(session, stale_token),
        },
    )


def gate_stale_credentials(
    session: ProofSession, stale_token: str, marker: str
) -> dict[str, object]:
    """Gate 9b: the previous session's token must not mutate the new session."""
    receipts, error = _send_and_listen(session, marker, token=stale_token)
    if error is not None:
        return {"error": error, "receipts": receipts}
    delivered = not _marker_absent_through(session, marker, timeout=10.0)
    return {
        "credentialAccepted": bool(receipts),
        "deliveredWithStaleToken": delivered,
        "receipts": receipts,
    }


def platform_label() -> str:
    """Bounded platform label, e.g. darwin-arm64."""
    machine = platform.machine().lower() or "unknown"
    return f"{platform.system().lower()}-{machine}"


@dataclass(frozen=True)
class MainGatesOutcome:
    """Facts the fixture record needs after the main session rotates."""

    idle_ok: bool
    turn_mapped: bool
    duplicate_semantics: str
    stale_socket: str
    stale_token: str


def _run_main_gates(
    main: ProofSession,
    unverified: ProofSession,
    own_child_marker: str,
    gates: list[GateResult],
) -> MainGatesOutcome:
    """Gates 1-9 on the accept-mode session; rotation stops and resumes it."""
    gates.append(gate_session_start(main))
    gates.append(gate_auth(main, unverified))
    idle = gate_idle_submission(main)
    idle_ok = count_marker(main, idle.marker) == 1
    gates.append(
        GateResult(
            "idle_submission",
            "pass" if idle_ok else "fail",
            {"visibleUserInputs": count_marker(main, idle.marker)},
        )
    )
    duplicate = gate_duplicate(main, idle)
    duplicate_ok = duplicate["duplicateSemantics"] in (
        "duplicate-suppressed",
        "duplicate-created-second-input",
    )
    gates.append(
        GateResult(
            "duplicate_msg_id",
            "pass" if duplicate_ok else "fail",
            {"duplicateSemantics": duplicate["duplicateSemantics"]},
        )
    )
    admission, mapping = gate_admission_and_turn_mapping(main, idle)
    gates.append(
        GateResult(
            "admission_fact",
            "pass" if admission["transcriptUserRecordIsAdmission"] else "fail",
            admission,
        )
    )
    turn_mapped = mapping["frameUuidEqualsRecordUuid"] is True
    gates.append(GateResult("turn_mapping", "pass" if turn_mapped else "fail", mapping))
    taxonomy = gate_failure_taxonomy(main, unverified)
    gates.append(taxonomy)
    busy = gate_busy(main)
    busy_classified = busy["classification"] != "unclassified"
    gates.append(
        GateResult(
            "busy_behavior",
            "pass" if busy_classified else "no-go",
            busy,
        )
    )
    own_child = gate_own_child(main, own_child_marker)
    gates.append(
        GateResult(
            "own_child_delivery",
            "pass" if own_child["hookPostedMarkerDelivered"] else "fail",
            own_child,
        )
    )
    outcome = MainGatesOutcome(
        idle_ok=idle_ok,
        turn_mapped=turn_mapped,
        duplicate_semantics=str(duplicate["duplicateSemantics"]),
        stale_socket=main.socket_path,
        stale_token=main.token,
    )
    rotation = gate_rotation(
        main, stale_socket=outcome.stale_socket, stale_token=outcome.stale_token
    )
    gates.append(rotation)
    return outcome


def assemble_record(
    facts: BinaryFacts, gates: list[GateResult], outcome: MainGatesOutcome
) -> dict[str, object]:
    """Build the complete schema-2 document; pure so tests can pin it."""
    names = [gate.name for gate in gates]
    missing = sorted(set(REQUIRED_GATE_NAMES) - set(names))
    unexpected = sorted(set(names) - set(REQUIRED_GATE_NAMES))
    duplicates = sorted({name for name in names if names.count(name) > 1})
    malformed = missing or unexpected or duplicates or len(names) != len(REQUIRED_GATE_NAMES)
    nogo = sorted(gate.name for gate in gates if gate.status == "no-go")
    failing = sorted(gate.name for gate in gates if gate.status == "fail")
    if malformed:
        result = (
            "no-go: invalid gate set "
            f"(missing={missing}, unexpected={unexpected}, duplicates={duplicates})"
        )
    elif nogo:
        result = f"no-go: required subcases unclassified: {', '.join(nogo)}"
        if failing:
            result += f"; failing gates: {', '.join(failing)}"
    elif not all(gate.passed for gate in gates):
        result = "fail: conformance gates did not all pass"
    else:
        result = (
            f"{PASS_RESULT_PREFIX} (claude {facts.version_text}; wire facts "
            f"statically inspected at {STATIC_INSPECTED_VERSION})"
        )
    return {
        "schema": 2,
        "inspected": {
            "binary": "<resolved claude executable>",
            "date": date.today().isoformat(),
            "platform": platform_label(),
            "sha256": facts.sha256,
            "sizeBytes": facts.size_bytes,
            "version": facts.version_text,
        },
        "staticInspection": {
            "buildTime": STATIC_BUILD_TIME,
            "gitSha": STATIC_GIT_SHA,
            "sameMachineFloor": ".".join(str(p) for p in SAME_MACHINE_FLOOR),
            "version": STATIC_INSPECTED_VERSION,
        },
        "gates": {
            gate.name: {
                "status": gate.status,
                **({"evidence": gate.evidence} if gate.evidence else {}),
            }
            for gate in gates
        },
        "idleSend": {
            "frameProven": outcome.idle_ok,
            "admissionReply": "transcript-user-record" if outcome.idle_ok else None,
            "turnIdField": "uuid" if outcome.turn_mapped else None,
            "duplicateSemantics": outcome.duplicate_semantics,
        },
        "busySend": "not-adopted",
        "result": result,
    }


def write_record_atomic(out_path: Path, record: dict, secrets: tuple[str, ...]) -> None:
    """Redact, verify, then publish a fresh output without overwriting."""
    rendered = json.dumps(redact(record, secrets), indent=2, sort_keys=True) + "\n"
    assert_no_secrets(rendered, secrets)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temp = out_path.with_name(f"{out_path.name}.tmp-{uuid_module.uuid4().hex[:8]}")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp, flags, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, out_path, follow_symlinks=False)
    except FileExistsError as exc:
        raise ConformanceError("conformance output must be a fresh path") from exc
    finally:
        with contextlib.suppress(OSError):
            temp.unlink()


def assert_no_secrets(text: str, secrets: tuple[str, ...]) -> None:
    """Refuse to record any frame or fixture that still carries a token."""
    for secret in secrets:
        if secret and secret in text:
            raise ConformanceError("fixture write would leak a messaging token")


def _validate_out_path(out_path: Path) -> None:
    """Live output must be a fresh path outside the immutable fixture tree."""
    if FIXTURE_TREE_COMPONENT in out_path.parts:
        raise ConformanceError(
            "conformance output must not write into a fixtures tree; copy a "
            "reviewed record into the fixture as a separate action"
        )
    if out_path.exists():
        raise ConformanceError("conformance output must be a fresh path")


def run_conformance(binary: str, *, out_path: Path) -> dict:
    """Execute every Phase 0 gate against one disposable stock binary run."""
    _validate_out_path(out_path)
    facts = binary_facts(binary)
    if facts is None:
        raise ConformanceError("stock binary did not report a version")
    if not facts.qualified:
        raise ConformanceError(
            f"stock binary {facts.version} is below the same-machine floor "
            f"{SAME_MACHINE_FLOOR}; the executable proof cannot run"
        )
    base = Path(tempfile.mkdtemp(prefix="theater-claude-proof-"))
    own_child_marker = f"theater-proof-own-child-{uuid_module.uuid4().hex[:8]}"
    secrets: list[str] = []
    gates: list[GateResult] = []
    main = ProofSession(facts.resolved, base_dir=base)
    unverified = ProofSession(facts.resolved, base_dir=base)
    try:
        main.start(
            settings_extra={"crossSessionInbound": "accept"},
            own_child_marker=own_child_marker,
        )
        unverified.start(
            settings_extra={"crossSessionInbound": "accept"},
            own_child_marker=None,
        )
        secrets.append(main.token)
        secrets.append(unverified.token)
        outcome = _run_main_gates(main, unverified, own_child_marker, gates)
        stale_marker = f"theater-proof-stale-{uuid_module.uuid4().hex[:8]}"
        stale = gate_stale_credentials(unverified, outcome.stale_token, stale_marker)
        gates.append(
            GateResult(
                "stale_credentials",
                (
                    "pass"
                    if not stale["credentialAccepted"] and not stale["deliveredWithStaleToken"]
                    else "fail"
                ),
                stale,
            )
        )
    finally:
        main.stop()
        unverified.stop()
        subprocess.run(
            ["tmux", "-S", str(base / "tmux.sock"), "kill-server"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(base, ignore_errors=True)
    record = assemble_record(facts, gates, outcome)
    write_record_atomic(out_path, record, tuple(secrets))
    return record


def write_no_go_fixture(
    out_path: Path,
    *,
    reason: str,
    extra: dict | None = None,
) -> None:
    """Record the honest machine-readable no-go for the current environment."""
    record = {
        "schema": 2,
        "gates": {
            name: {"status": "not-run", "reason": reason}
            for name in (
                "session_start",
                "auth",
                "idle_submission",
                "duplicate_msg_id",
                "admission_fact",
                "turn_mapping",
                "failure_taxonomy",
                "busy_behavior",
                "own_child_delivery",
                "session_rotation",
                "stale_credentials",
            )
        },
        "idleSend": {
            "frameProven": False,
            "admissionReply": None,
            "turnIdField": None,
            "duplicateSemantics": "unproven",
        },
        "busySend": "not-adopted",
        "result": f"no-go: {reason}",
        **(extra or {}),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
