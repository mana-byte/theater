"""Wave 0 proof: the native Codex topology and compatibility gate (plan §3.2).

This module proves, against the unmodified installed Codex release, that the
frozen Theater topology is real:

* ``codex app-server --listen unix://<private-socket>`` detached backend,
* WebSocket frames with an HTTP Upgrade handshake over the Unix socket,
* requests that omit the jsonrpc version field plus initialize/initialized,
* one native CLI UI (``codex --remote unix://<socket> resume <thread-id>``)
  and one Theater-like observing/control client attached to the SAME live
  backend/thread,
* approval requests broadcast to thread subscribers that the observer must
  never answer, new/fork/reconnect behavior, event-based UI readiness,
* steering with expectedTurnId, interruption, capability-gated settings,
* backend and UI survival across abrupt control-client death,
* the native-UI concurrent submission race and the turn identity Theater's
  turn/start actually receives, and that an explicit ``--remote`` is
  necessary and never silently falls back to an embedded backend.

Two layers:

* offline conformance tests run under ordinary pytest and assert the
  sanitized fixtures captured from the real release (including schema files
  emitted by the installed binary itself);
* ``test_native_smoke_*`` tests drive the real release. They are opt-in: run
  them with the deterministic invocation

      THEATER_CODEX_NATIVE_PROOF=1 \\
          uv run pytest tests/test_codex_native_runtime_proof.py -k native_smoke -v

  Ordinary pytest skips them; skipped smoke tests do not satisfy the gate.

Nothing here is production runtime code.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from tests.native.codex_native_client import (
    MAX_FRAME_BYTES,
    AppServerProcess,
    FrameTooLarge,
    NativeWebSocketClient,
    TmuxUi,
    decode_frames,
    encode_frame,
    expected_accept,
    wait_until,
    write_isolated_codex_home,
)

FIXTURES = Path(__file__).parent / "fixtures" / "codex_native_runtime"
SCHEMA = FIXTURES / "protocol_schema"
NATIVE_PROOF_ENV = "THEATER_CODEX_NATIVE_PROOF"
NATIVE_PROOF_ENABLED = os.environ.get(NATIVE_PROOF_ENV) == "1"
EXPECTED_VERSION = "0.154.0"

REQUIRED_CLIENT_METHODS = (
    "initialize",
    "thread/start",
    "thread/resume",
    "thread/fork",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "thread/settings/update",
    "thread/queue/add",
)
REQUIRED_APPROVAL_METHODS = (
    "item/commandExecution/requestApproval",
    "item/fileChange/requestApproval",
    "execCommandApproval",
    "applyPatchApproval",
)
REQUIRED_NOTIFICATIONS = (
    "turn/started",
    "turn/completed",
    "thread/status/changed",
    "item/completed",
    "item/agentMessage/delta",
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def load_schema(name: str) -> dict:
    return json.loads((SCHEMA / name).read_text())


def schema_methods(schema: dict) -> list[str]:
    methods: list[str] = []
    for variant in schema.get("oneOf", []):
        enum = variant.get("properties", {}).get("method", {}).get("enum")
        if enum:
            methods.append(enum[0])
    return methods


# ---------------------------------------------------------------------------
# Offline conformance — fixtures captured from the real installed release
# ---------------------------------------------------------------------------


class TestProtocolSurfaceConformance:
    def test_installed_release_facts(self) -> None:
        facts = load_fixture("installed_release.json")
        assert facts["installed_version"] == f"codex-cli {EXPECTED_VERSION}"
        assert facts["schema_generation"]["command"].startswith("codex app-server")

    def test_required_client_methods_present(self) -> None:
        methods = schema_methods(load_schema("ClientRequest.json"))
        for method in REQUIRED_CLIENT_METHODS:
            assert method in methods, f"{method} missing from installed-release schema"

    def test_required_approval_methods_present(self) -> None:
        methods = schema_methods(load_schema("ServerRequest.json"))
        for method in REQUIRED_APPROVAL_METHODS:
            assert method in methods, f"{method} missing from installed-release schema"

    def test_required_notifications_present(self) -> None:
        methods = schema_methods(load_schema("ServerNotification.json"))
        for method in REQUIRED_NOTIFICATIONS:
            assert method in methods, f"{method} missing from installed-release schema"

    def test_wire_messages_omit_jsonrpc_version(self) -> None:
        request = load_schema("JSONRPCRequest.json")
        assert "jsonrpc" not in request["properties"]
        assert set(request["required"]) == {"id", "method"}
        message = load_schema("JSONRPCMessage.json")
        for variant in message["anyOf"]:
            definition = message["definitions"][variant["$ref"].split("/")[-1]]
            assert "jsonrpc" not in definition["properties"]
        assert {variant["$ref"].split("/")[-1] for variant in message["anyOf"]} == {
            "JSONRPCRequest",
            "JSONRPCNotification",
            "JSONRPCResponse",
            "JSONRPCError",
        }

    def test_initialize_initialized_is_the_only_client_notification(self) -> None:
        methods = schema_methods(load_schema("ClientNotification.json"))
        assert methods == ["initialized"]

    def test_turn_method_shapes(self) -> None:
        schema = load_schema("ClientRequest.json")
        defs = schema["definitions"]

        steer = self._params_of(schema, "turn/steer")
        steer_def = defs[steer["$ref"].split("/")[-1]]
        assert steer_def["required"] == ["expectedTurnId", "input", "threadId"]

        interrupt = self._params_of(schema, "turn/interrupt")
        interrupt_def = defs[interrupt["$ref"].split("/")[-1]]
        assert interrupt_def["required"] == ["threadId", "turnId"]

        start = self._params_of(schema, "turn/start")
        start_def = defs[start["$ref"].split("/")[-1]]
        assert "threadId" in start_def["required"]
        assert "input" in start_def["required"]
        # turn/start carries no expectedTurnId: the server steers internally.
        assert "expectedTurnId" not in start_def["properties"]

        settings = self._params_of(schema, "thread/settings/update")
        settings_def = defs[settings["$ref"].split("/")[-1]]
        assert "threadId" in settings_def["required"]
        assert {"model", "effort"} <= set(settings_def["properties"])

    @staticmethod
    def _params_of(schema: dict, method: str) -> dict:
        for variant in schema["oneOf"]:
            if variant["properties"]["method"]["enum"] == [method]:
                return variant["properties"]["params"]
        raise AssertionError(f"{method} not found")

    def test_approval_decision_surface(self) -> None:
        schema = load_schema("ServerRequest.json")
        decision = schema["definitions"]["CommandExecutionApprovalDecision"]
        decisions = set()
        for variant in decision["oneOf"]:
            if "enum" in variant:
                decisions.add(variant["enum"][0])
            else:
                decisions.update(variant.get("properties", {}).keys())
        assert decisions == {
            "accept",
            "acceptForSession",
            "acceptWithExecpolicyAmendment",
            "applyNetworkPolicyAmendment",
            "decline",
            "cancel",
        }
        fixture = load_fixture("approval.json")
        assert set(fixture["approval_request"]["valid_decisions"]) == decisions


class TestCapturedBehaviorConformance:
    """Assert the recorded live-capture facts stay internally consistent."""

    def test_handshake_facts(self) -> None:
        fixture = load_fixture("handshake.json")
        assert fixture["transport"].startswith("WebSocket")
        assert fixture["server_response_status_line"] == "HTTP/1.1 101 Switching Protocols"
        assert fixture["client_request_line"] == "GET / HTTP/1.1"
        assert fixture["initialize"]["required_notification"] == {"method": "initialized"}
        assert "jsonrpc" in fixture["jsonrpc_version_field"]

    def test_handshake_accept_derivation_known_answer(self) -> None:
        # RFC 6455 sample key/value pair.
        assert expected_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

    def test_thread_lifecycle_facts(self) -> None:
        fixture = load_fixture("thread_lifecycle.json")
        resume = fixture["thread_resume"]
        assert resume["subscribes_second_connection"] is True
        assert "turn/started" in resume["live_notification_methods_seen_by_second_client"]
        assert resume["error_before_rollout_exists"]["code"] == -32600
        assert fixture["thread_start"]["thread_facts"]["rollout_exists_before_first_turn"] is False
        assert fixture["thread_start"]["thread_facts"]["rollout_exists_after_first_turn"] is True
        fork = fixture["thread_fork"]
        assert fork["history_preserved"] is True
        assert "forked_from_id" in fork

    def test_turn_control_facts(self) -> None:
        fixture = load_fixture("turn_control.json")
        race = fixture["concurrent_submission_race"]
        assert race["response_turn_id_equals_active_turn_id"] is True
        steer = fixture["turn_steer"]
        assert steer["expectedTurnId_required"] is True
        assert steer["response"] == {"result": {"turnId": "<same active turn-id>"}}
        assert steer["stale_turn_refusal"]["error"]["code"] == -32600
        assert steer["stale_turn_refusal"]["error"]["message"] == "no active turn to steer"
        interrupt = fixture["turn_interrupt"]
        assert interrupt["completed_turn_status"] == "interrupted"
        assert interrupt["completed_turn_error"] is None

    def test_approval_facts(self) -> None:
        fixture = load_fixture("approval.json")
        assert (
            fixture["approval_request"]["method_fired"] == "item/commandExecution/requestApproval"
        )
        assert fixture["broadcast_behavior"]["delivered_to_every_subscribed_connection"] is True
        assert fixture["resolution"]["notification"] == "serverRequest/resolved"
        assert "never" in fixture["observer_rule"]

    def test_capability_gating_facts(self) -> None:
        fixture = load_fixture("capabilities.json")
        settings = fixture["thread_settings_update"]
        assert settings["without_experimental_api"]["error"] == {
            "code": -32600,
            "message": "thread/settings/update requires experimentalApi capability",
        }
        queue = fixture["native_queue"]
        assert queue["without_experimental_api"]["add_error"]["code"] == -32600
        assert "not use the native queue" in queue["theater_position"]

    def test_ui_topology_facts(self) -> None:
        fixture = load_fixture("ui_topology.json")
        assert fixture["topology"]["backend"].startswith("codex app-server --listen unix://")
        assert fixture["topology"]["ui"].startswith("codex --remote unix://")
        assert fixture["shared_session"]["observer_saw_ui_initiated"]
        assert "no_blind_sleep" in fixture["ui_readiness"]
        survival = fixture["survival"]["abrupt_control_client_death"]
        assert survival["backend_alive"] and survival["ui_alive"]
        assert survival["reconnect"].startswith("a fresh control client")
        remote = fixture["remote_flag"]
        assert "no embedded backend was started" in remote["bad_endpoint"]["no_silent_fallback"]

    def test_unsupported_capabilities_have_explicit_reasons(self) -> None:
        fixture = load_fixture("unsupported_capabilities.json")
        assert len(fixture["entries"]) >= 5
        for entry in fixture["entries"]:
            assert entry["status"] in {"unavailable", "not used by design", "not exercised"}
            assert entry["reason"], f"missing reason for {entry['capability']}"

    def test_zero_turn_error_consistent_across_fixtures(self) -> None:
        lifecycle = load_fixture("thread_lifecycle.json")
        topology = load_fixture("ui_topology.json")
        assert (
            "no rollout found"
            in lifecycle["thread_resume"]["native_ui_resume_before_first_turn_error"]
        )
        assert "no rollout found" in topology["remote_flag"]["bad_endpoint"]["error_text"]


# ---------------------------------------------------------------------------
# Offline conformance — the framing helper itself
# ---------------------------------------------------------------------------


class TestFramingCodec:
    def test_roundtrip_all_length_classes(self) -> None:
        for size in (0, 5, 125, 126, 65535, 65536, 100_000):
            payload = os.urandom(size)
            frame = encode_frame(payload, mask=True)
            assert frame[1] & 0x80, "client frames must be masked"
            frames, remainder = decode_frames(frame)
            assert remainder == b""
            assert len(frames) == 1
            opcode, decoded = frames[0]
            assert opcode == 0x1
            assert decoded == payload

    def test_multiple_frames_in_one_buffer(self) -> None:
        buffer = encode_frame(b'{"a": 1}', mask=False) + encode_frame(b'{"b": 2}', mask=False)
        buffer += b"partial"
        frames, remainder = decode_frames(buffer)
        assert [payload for _, payload in frames] == [b'{"a": 1}', b'{"b": 2}']
        assert remainder == b"partial"

    def test_oversized_frame_rejected(self) -> None:
        header = bytearray([0x81, 0x80 | 127]) + (MAX_FRAME_BYTES + 1).to_bytes(8, "big")
        with pytest.raises(FrameTooLarge):
            decode_frames(bytes(header))

    def test_client_handshake_shape(self) -> None:
        from tests.native.codex_native_client import build_client_handshake

        request, key = build_client_handshake()
        text = request.decode()
        assert text.startswith("GET / HTTP/1.1\r\n")
        assert "Upgrade: websocket\r\n" in text
        assert "Connection: Upgrade\r\n" in text
        assert f"Sec-WebSocket-Key: {key}\r\n" in text
        assert "Sec-WebSocket-Version: 13\r\n" in text
        base64.b64decode(key)  # the key must be valid base64


class TestClientAgainstFakeServer:
    """Exercise NativeWebSocketClient offline against a scripted server."""

    @pytest.fixture()
    def fake_server(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        path = Path(f"/tmp/theater-proof-fake-{os.getpid()}-{uuid.uuid4().hex[:6]}.sock")
        listener.bind(str(path))
        listener.listen(1)
        received: list[dict] = []
        got_request = threading.Event()

        def serve() -> None:
            conn, _ = listener.accept()
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            accept = expected_accept(data.decode().split("Sec-WebSocket-Key: ")[1].split("\r\n")[0])
            conn.sendall(
                (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "connection: Upgrade\r\n"
                    "upgrade: websocket\r\n"
                    f"sec-websocket-accept: {accept}\r\n\r\n"
                ).encode()
            )
            # A ping the client must answer, then the initialize response.
            conn.sendall(encode_frame(b"ping-payload", 0x9, mask=False))
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                frames, _ = decode_frames(chunk)
                for opcode, payload in frames:
                    if opcode in (0x1, 0x2):
                        received.append(json.loads(payload))
                        got_request.set()
                if got_request.is_set():
                    conn.sendall(encode_frame(b'{"id": 1, "result": {"ok": true}}', mask=False))
                    conn.close()
                    return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        yield path, received
        listener.close()
        path.unlink(missing_ok=True)

    def test_handshake_request_and_framing(self, fake_server) -> None:
        path, received = fake_server
        client = NativeWebSocketClient(path)
        assert client.handshake.status_line == "HTTP/1.1 101 Switching Protocols"
        assert client.handshake.accept_valid is True
        assert client.handshake.headers["upgrade"] == "websocket"
        response = client.request("initialize", {"clientInfo": {"name": "x", "version": "0.1"}})
        assert response == {"id": 1, "result": {"ok": True}}
        assert received and received[0]["method"] == "initialize"
        assert "jsonrpc" not in received[0]
        client.close()


# ---------------------------------------------------------------------------
# Real-native smoke tests — opt-in, deterministic invocation
# ---------------------------------------------------------------------------

native_proof_required = pytest.mark.skipif(
    not NATIVE_PROOF_ENABLED,
    reason=f"opt-in real-native proof: run with {NATIVE_PROOF_ENV}=1 and -k native_smoke",
)
needs_codex = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex release binary not on PATH",
)
needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux is required to attach the native TUI",
)


@pytest.fixture(scope="module")
def native_env():
    """One isolated backend per module; every test owns a fresh thread."""
    if not NATIVE_PROOF_ENABLED:
        pytest.skip(f"set {NATIVE_PROOF_ENV}=1 to run real-native smoke tests")
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="codex-proof-", dir="/tmp"))
    repo = root / "repo"
    repo.mkdir()
    codex_home = write_isolated_codex_home(root, trusted_paths=[repo])
    app_server = AppServerProcess.spawn(
        codex_home=codex_home,
        socket_path=root / "control.sock",
        log_path=root / "app-server.log",
    )
    registry: dict[str, object] = {"tmux": None}

    class Env:
        def __init__(self) -> None:
            self.root = root
            self.repo = repo
            self.socket_path = root / "control.sock"
            self.app_server = app_server

        def connect(self, *, experimental: bool = False) -> NativeWebSocketClient:
            client = NativeWebSocketClient(self.socket_path)
            client.initialize(experimental=experimental)
            return client

        def tmux(self, name: str) -> TmuxUi:
            existing = registry.get("tmux")
            if existing is not None:
                existing.kill()
            ui = TmuxUi(root / f"tmux-{name}.sock")
            registry["tmux"] = ui
            return ui

    yield Env()
    ui = registry.get("tmux")
    if ui is not None:
        ui.kill()
    app_server.terminate()
    shutil.rmtree(root, ignore_errors=True)


def launch_ui(env, ui: TmuxUi, thread_id: str) -> None:
    """The frozen topology's frontend command: attach the native CLI UI."""
    home = env.root / "home"
    ui.launch(f"CODEX_HOME={home} codex --remote unix://{env.socket_path} resume {thread_id}")


def start_thread(env) -> tuple[NativeWebSocketClient, str]:
    client = env.connect()
    response = client.request("thread/start", {"cwd": str(env.repo)})
    return client, response["result"]["thread"]["id"]


def run_turn(
    client: NativeWebSocketClient, thread_id: str, prompt: str, *, timeout: float = 180.0
) -> dict:
    response = client.request(
        "turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]}
    )
    turn_id = response["result"]["turn"]["id"]
    completed = client.wait_notification("turn/completed", timeout=timeout)
    assert completed["params"]["turn"]["id"] == turn_id
    return completed


def wait_active(client: NativeWebSocketClient, thread_id: str, *, timeout: float = 90.0) -> None:
    """Wait for the thread to reach active state; deadline-bounded state drain.

    Only statuses that arrive after the call are considered: earlier turns
    leave ``active`` statuses in the client's buffer, and matching one of
    those would treat a not-yet-started turn as already running.
    """
    mark = len(client.notifications)
    wait_until(
        lambda: _drain_active(client, mark),
        timeout=timeout,
        what="thread to become active",
    )


def _drain_active(client: NativeWebSocketClient, mark: int) -> bool:
    client.drain(quiet=1.0)
    return any(
        message.get("method") == "thread/status/changed"
        and message["params"].get("status", {}).get("type") == "active"
        for message in client.notifications[mark:]
    )


@native_proof_required
@needs_codex
class TestNativeSmokeHandshakeAndFraming:
    def test_native_smoke_handshake_and_version(self, native_env) -> None:
        client = NativeWebSocketClient(native_env.socket_path)
        assert client.handshake.status_line == "HTTP/1.1 101 Switching Protocols"
        assert client.handshake.accept_valid is True
        assert "websocket" in client.handshake.headers["upgrade"]

        installed = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        assert installed == f"codex-cli {EXPECTED_VERSION}", installed

        response = client.initialize()
        result = response["result"]
        assert EXPECTED_VERSION in result["userAgent"]
        assert result["platformFamily"] == "unix"
        client.notify("initialized")
        client.close()

    def test_native_smoke_new_thread_and_rollout_lifecycle(self, native_env) -> None:
        client, thread_id = start_thread(native_env)
        assert uuid.UUID(thread_id)  # a real id, not fabricated
        response = client.request("thread/read", {"threadId": thread_id})
        thread = response["result"]["thread"]
        assert thread["id"] == thread_id
        assert thread["cliVersion"] == EXPECTED_VERSION
        assert thread["status"]["type"] == "idle"
        rollout = Path(thread["path"])
        assert not rollout.exists()  # no rollout before the first turn

        completed = run_turn(client, thread_id, "Reply with exactly: alpha")
        assert completed["params"]["turn"]["status"] == "completed"
        assert rollout.exists()  # the rollout appears once a turn is accepted
        client.close()


@native_proof_required
@needs_codex
@needs_tmux
class TestNativeSmokeSharedUiSession:
    def test_native_smoke_shared_ui_session_and_observer(self, native_env) -> None:
        env = native_env
        control, thread_id = start_thread(env)
        marker = "marble-codex-proof-marker"
        run_turn(control, thread_id, f"Remember the phrase {marker}. Reply with exactly: ok")

        observer = env.connect()
        resumed = observer.request("thread/resume", {"threadId": thread_id})
        assert resumed["result"]["thread"]["id"] == thread_id

        ui = env.tmux("codex-native-proof-ui")
        launch_ui(env, ui, thread_id)
        ui.wait_ready(marker)  # state-based: history rendered + composer visible

        ui.type_and_submit("Reply with exactly: shared-ui")

        started = observer.wait_notification("turn/started", timeout=120)
        assert started["params"]["threadId"] == thread_id
        ui_turn_id = started["params"]["turn"]["id"]

        completed = observer.wait_notification("turn/completed", timeout=180)
        assert completed["params"]["turn"]["id"] == ui_turn_id
        assert completed["params"]["turn"]["status"] == "completed"
        answers = [
            item["text"]
            for item in completed["params"]["turn"].get("items", [])
            if item.get("type") == "agentMessage"
        ]
        assert answers and "shared-ui" in answers[-1]
        control.close()
        observer.close()

    def test_native_smoke_approval_ownership_with_real_ui(self, native_env) -> None:
        env = native_env
        # Approvals are only requested when the sandbox would block the command:
        # the trusted repo workspace runs in-workspace writes silently, so the
        # approval thread runs in an untrusted (read-only sandbox) directory.
        untrusted = env.root / "approval-untrusted"
        untrusted.mkdir()
        control = env.connect()
        thread_id = control.request("thread/start", {"cwd": str(untrusted)})["result"]["thread"][
            "id"
        ]
        marker = "approval-proof-marker"
        run_turn(control, thread_id, f"Remember the phrase {marker}. Reply with exactly: ok")

        observer = env.connect()
        observer.request("thread/resume", {"threadId": thread_id})

        ui = env.tmux("codex-native-proof-approval")
        launch_ui(env, ui, thread_id)
        ui.wait_ready(marker)

        target = untrusted / "approval-proof.txt"
        target.unlink(missing_ok=True)
        prompt = (
            f"Use the shell tool to run exactly this command now: touch {target}. "
            "You must actually run the command with the shell tool; do not reply "
            "before it has run."
        )
        control.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]},
        )

        # Both subscribers receive the approval request; the observer never answers.
        observer_request = observer.wait_server_request(
            "item/commandExecution/requestApproval", timeout=240
        )
        assert observer_request["params"]["threadId"] == thread_id
        assert observer_request["params"]["turnId"]

        wait_until(
            lambda: "Allow creating" in ui.pane() or str(target) in ui.pane(),
            timeout=240,
            what="approval dialog in the native UI",
        )
        ui.send_keys("y")

        wait_until(target.exists, timeout=240, what="approved command to execute")
        # The observer never sent any approval response: it only received the
        # request. The native UI answered, so the observer learns the outcome
        # second-hand from the server.
        resolved = observer.wait_notification("serverRequest/resolved", timeout=60)
        assert resolved is not None
        completed = observer.wait_notification("turn/completed", timeout=180)
        assert completed["params"]["turn"]["status"] == "completed"
        control.close()
        observer.close()


@native_proof_required
@needs_codex
class TestNativeSmokeTurnControls:
    def test_native_smoke_steer_interrupt_and_race(self, native_env) -> None:
        control, thread_id = start_thread(native_env)

        # Steering amends the active turn; the turn id never changes.
        response = control.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {"type": "text", "text": "Write a long, detailed 1200 word essay about rivers."}
                ],
            },
        )
        active_id = response["result"]["turn"]["id"]
        wait_active(control, thread_id)
        steered = control.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": active_id,
                "input": [
                    {
                        "type": "text",
                        "text": "Stop writing the essay immediately. Reply with exactly: steered",
                    }
                ],
            },
            timeout=60,
        )
        assert steered["result"]["turnId"] == active_id
        completed = control.wait_notification("turn/completed", timeout=300)
        assert completed["params"]["turn"]["id"] == active_id

        # A stale steer is a refusal, never a new send.
        stale = control.request(
            "turn/steer",
            {
                "threadId": thread_id,
                "expectedTurnId": active_id,
                "input": [{"type": "text", "text": "ignored"}],
            },
            timeout=60,
        )
        assert stale["error"]["code"] == -32600
        assert stale["error"]["message"] == "no active turn to steer"

        # turn/start during an active turn absorbs the message into that turn.
        response = control.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Write a 1200 word essay about mountains."}],
            },
        )
        race_id = response["result"]["turn"]["id"]
        wait_active(control, thread_id)
        raced = control.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [
                    {
                        "type": "text",
                        "text": "Never mind the essay. Reply with exactly: raced",
                    }
                ],
            },
        )
        assert raced["result"]["turn"]["id"] == race_id  # start_or_steer_turn
        completed = control.wait_notification("turn/completed", timeout=300)
        assert completed["params"]["turn"]["id"] == race_id

        # Interrupt cancels the active turn; the participant stays alive.
        response = control.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": "Write a 1200 word essay about the sea."}],
            },
        )
        sea_id = response["result"]["turn"]["id"]
        wait_active(control, thread_id)
        control.request("turn/interrupt", {"threadId": thread_id, "turnId": sea_id})
        completed = control.wait_notification("turn/completed", timeout=180)
        assert completed["params"]["turn"]["id"] == sea_id
        assert completed["params"]["turn"]["status"] == "interrupted"
        control.close()

    def test_native_smoke_settings_capability_gating(self, native_env) -> None:
        control, thread_id = start_thread(native_env)

        denied = control.request(
            "thread/settings/update", {"threadId": thread_id, "effort": "medium"}
        )
        assert denied["error"]["code"] == -32600
        assert denied["error"]["message"] == (
            "thread/settings/update requires experimentalApi capability"
        )

        experimental = native_env.connect(experimental=True)
        allowed = experimental.request(
            "thread/settings/update", {"threadId": thread_id, "effort": "medium"}
        )
        assert allowed.get("result") == {}
        readback = experimental.request("thread/read", {"threadId": thread_id})
        assert readback["result"]["thread"]["reasoningEffort"] == "medium"

        queue_denied = control.request(
            "thread/queue/add",
            {
                "threadId": thread_id,
                "clientUserMessageId": "proof-1",
                "input": [{"type": "text", "text": "queued"}],
            },
        )
        assert queue_denied["error"]["code"] == -32600
        assert "requires experimentalApi capability" in queue_denied["error"]["message"]
        control.close()
        experimental.close()


@native_proof_required
@needs_codex
class TestNativeSmokeForkReconnectSurvival:
    def test_native_smoke_fork(self, native_env) -> None:
        control, thread_id = start_thread(native_env)
        run_turn(control, thread_id, "Remember the word marble. Reply with exactly: ok")

        forked = control.request("thread/fork", {"threadId": thread_id})
        fork_thread = forked["result"]["thread"]
        assert fork_thread["id"] != thread_id
        assert fork_thread["forkedFromId"] == thread_id
        control.close()

    def test_native_smoke_backend_survives_abrupt_client_death(self, native_env) -> None:
        env = native_env
        control, thread_id = start_thread(env)
        marker = "survival-proof-marker"
        run_turn(control, thread_id, f"Remember the phrase {marker}. Reply with exactly: ok")
        control.close_abrupt()

        assert env.app_server.alive()  # backend survives daemon-side connection death

        reconnected = env.connect()
        resumed = reconnected.request("thread/resume", {"threadId": thread_id})
        assert resumed["result"]["thread"]["id"] == thread_id
        reconnected.close()

    def test_native_smoke_ui_survives_and_works_after_reconnect(self, native_env) -> None:
        env = native_env
        control, thread_id = start_thread(env)
        marker = "ui-survival-marker"
        run_turn(control, thread_id, f"Remember the phrase {marker}. Reply with exactly: ok")

        ui = env.tmux("codex-native-proof-survival")
        launch_ui(env, ui, thread_id)
        ui.wait_ready(marker)

        control.close_abrupt()  # daemon connection dies while the UI is attached

        assert env.app_server.alive()
        assert len(ui.pane().strip()) > 0  # the native UI is still rendering

        reconnected = env.connect()
        resumed = reconnected.request("thread/resume", {"threadId": thread_id})
        assert resumed["result"]["thread"]["id"] == thread_id

        ui.type_and_submit("Reply with exactly: survived")
        completed = reconnected.wait_notification("turn/completed", timeout=180)
        assert completed["params"]["threadId"] == thread_id
        answers = [
            item["text"]
            for item in completed["params"]["turn"].get("items", [])
            if item.get("type") == "agentMessage"
        ]
        assert answers and "survived" in answers[-1]
        reconnected.close()


@native_proof_required
@needs_codex
@needs_tmux
class TestNativeSmokeRemoteNecessity:
    def test_native_smoke_bad_remote_exits_without_fallback(self, native_env) -> None:
        env = native_env
        control, thread_id = start_thread(env)

        ui = env.tmux("codex-native-proof-badremote")
        ui.launch(
            f"CODEX_HOME={env.root / 'home'} "
            f"codex --remote unix://{env.root / 'missing.sock'} resume {thread_id}"
        )
        # The TUI must exit instead of silently starting an embedded backend.
        wait_until(lambda: not ui.session_alive(), timeout=60, what="bad-remote TUI to exit")

        # Our backend is untouched and still ours alone.
        response = control.request("thread/list", {})
        assert "result" in response
        control.close()
