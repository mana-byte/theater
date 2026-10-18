"""Opt-in real-codex native smoke: handshake, shared UI, turn controls, fork and reconnect."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from tests.native.codex_mock_provider import MockSession, standard_rules
from tests.native.codex_native_client import (
    AppServerProcess,
    NativeWebSocketClient,
    TmuxUi,
    launch_remote_ui,
    mock_provider_requested,
    qualification_codex_binary,
    qualification_version_override,
    run_turn,
    start_app_server_thread,
    wait_thread_active,
    wait_until,
    write_isolated_codex_home,
)
from theater.harness.builtin.plugins.codex.runtime_plan import (
    codex_version_refusal,
    parse_codex_version,
    qualified_versions_range_text,
)

pytestmark = pytest.mark.integration


NATIVE_PROOF_ENV = "THEATER_CODEX_NATIVE_PROOF"


NATIVE_PROOF_ENABLED = os.environ.get(NATIVE_PROOF_ENV) == "1"


VERSION_PROBE_TIMEOUT_SECONDS = 30.0


def require_installed_allowed_version() -> str:
    """The smoke gate: only a release inside the qualified range may run.

    THEATER_CODEX_QUALIFY_VERSION admits exactly one candidate version for
    this process only, so a release can be exercised BEFORE the ceiling
    bump (the bump stays a separate human-reviewed commit, last).
    THEATER_CODEX_NATIVE_BIN targets a specific binary such as a release
    fetched by tests/native/codex_fetch_release.py.
    """
    try:
        completed = subprocess.run(
            [qualification_codex_binary(), "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise AssertionError(
            f"codex --version did not answer within {VERSION_PROBE_TIMEOUT_SECONDS}s;"
            " a hung release must fail the gate, not hang it"
        ) from error
    except (subprocess.CalledProcessError, OSError) as error:
        raise AssertionError(
            f"codex --version failed ({error}); the opt-in gate needs a working "
            "codex release on PATH"
        ) from error
    output = completed.stdout.strip()
    version = parse_codex_version(output)
    assert version is not None, f"no codex-cli version in {output!r}"
    refusal = codex_version_refusal(version)
    assert refusal is None or version == qualification_version_override(), (
        f"{refusal}; qualified range {qualified_versions_range_text()}"
    )
    return version


native_proof_required = pytest.mark.skipif(
    not NATIVE_PROOF_ENABLED,
    reason=f"opt-in real-native proof: run with {NATIVE_PROOF_ENV}=1 and -k native_smoke",
)


def codex_binary_available() -> bool:
    override = os.environ.get("THEATER_CODEX_NATIVE_BIN")
    if override:
        return Path(override).is_file()
    return shutil.which("codex") is not None


needs_codex = pytest.mark.skipif(
    not codex_binary_available(),
    reason="no codex release binary (PATH or THEATER_CODEX_NATIVE_BIN)",
)


needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux is required to attach the native TUI",
)


@pytest.fixture(scope="module")
def native_env():
    """One isolated backend per module; every test owns a fresh thread.

    THEATER_CODEX_NATIVE_PROVIDER=mock wires the backend at the offline
    scripted provider instead of the ambient one: the same stock binary and
    wire protocol, no network and no model quota. THEATER_CODEX_NATIVE_BIN
    targets a specific release binary.
    """
    if not NATIVE_PROOF_ENABLED:
        pytest.skip(f"set {NATIVE_PROOF_ENV}=1 to run real-native smoke tests")
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="codex-proof-", dir="/tmp"))
    repo = root / "repo"
    repo.mkdir()
    mock: MockSession | None = None
    if mock_provider_requested():
        mock = MockSession.open(root, trusted_paths=[repo], rules=standard_rules())
        codex_home = mock.codex_home
    else:
        codex_home = write_isolated_codex_home(root, trusted_paths=[repo])
    app_server = AppServerProcess.spawn(
        codex_home=codex_home,
        socket_path=root / "control.sock",
        log_path=root / "app-server.log",
        codex_binary=qualification_codex_binary(),
    )
    registry: dict[str, TmuxUi | None] = {"tmux": None}

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
    if mock is not None:
        mock.close()
    shutil.rmtree(root, ignore_errors=True)


def launch_ui(env, ui: TmuxUi, thread_id: str) -> None:
    """The frozen topology's frontend command: attach the native CLI UI."""
    launch_remote_ui(
        ui,
        codex_home=env.root / "home",
        socket_path=env.socket_path,
        thread_id=thread_id,
        codex_binary=qualification_codex_binary(),
    )


def start_thread(env) -> tuple[NativeWebSocketClient, str]:
    client = env.connect()
    return client, start_app_server_thread(client, env.repo)


@native_proof_required
@needs_codex
class TestNativeSmokeHandshakeAndFraming:
    def test_native_smoke_handshake_and_version(self, native_env) -> None:
        client = NativeWebSocketClient(native_env.socket_path)
        assert client.handshake.status_line == "HTTP/1.1 101 Switching Protocols"
        assert client.handshake.accept_valid is True
        assert "websocket" in client.handshake.headers["upgrade"]

        installed = subprocess.run(
            [qualification_codex_binary(), "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()
        version = require_installed_allowed_version()
        assert installed == f"codex-cli {version}", installed

        response = client.initialize()
        result = response["result"]
        assert version in result["userAgent"]
        assert result["platformFamily"] == "unix"
        client.notify("initialized")
        client.close()

    def test_native_smoke_new_thread_and_rollout_lifecycle(self, native_env) -> None:
        client, thread_id = start_thread(native_env)
        assert uuid.UUID(thread_id)  # a real id, not fabricated
        response = client.request("thread/read", {"threadId": thread_id})
        thread = response["result"]["thread"]
        assert thread["id"] == thread_id
        assert thread["cliVersion"] == require_installed_allowed_version()
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
            lambda: f"$ touch {target}" in ui.pane(),
            timeout=240,
            what="approval dialog in the native UI",
        )
        ui.send_keys("Enter")

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
        wait_thread_active(control, thread_id)
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
        wait_thread_active(control, thread_id)
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
        wait_thread_active(control, thread_id)
        control.request("turn/interrupt", {"threadId": thread_id, "turnId": sea_id})
        completed = control.wait_notification("turn/completed", timeout=180)
        assert completed["params"]["turn"]["id"] == sea_id
        assert completed["params"]["turn"]["status"] == "interrupted"
        control.close()

    def test_native_smoke_settings_capability_gating(self, native_env) -> None:
        # The updater must be the thread's subscribed connection: the app-server
        # delivers thread/settings/updated only to a subscribed updater, which
        # is the production topology the broadcast confirmation relies on.
        experimental = native_env.connect(experimental=True)
        thread_id = start_app_server_thread(experimental, native_env.repo)

        plain = native_env.connect()
        denied = plain.request(
            "thread/settings/update", {"threadId": thread_id, "effort": "medium"}
        )
        assert denied["error"]["code"] == -32600
        assert denied["error"]["message"] == (
            "thread/settings/update requires experimentalApi capability"
        )

        allowed = experimental.request(
            "thread/settings/update", {"threadId": thread_id, "effort": "medium"}
        )
        assert allowed.get("result") == {}
        readback = experimental.request("thread/read", {"threadId": thread_id})
        thread = readback["result"]["thread"]
        release = tuple(int(part) for part in require_installed_allowed_version().split("."))
        if release >= (0, 153, 0):
            # Historical 0.153.0+ dialect: readback must reflect the applied effort, so a
            # release that drops it cannot qualify through the broadcast branch.
            assert thread.get("reasoningEffort") == "medium"
        else:
            # Pre-0.153 dialect: thread/read never carries effort; the applied-
            # settings broadcast is the confirmation, exactly as production does.
            assert "reasoningEffort" not in thread
            updated = experimental.wait_notification("thread/settings/updated", timeout=60)
            assert updated["params"]["threadSettings"]["effort"] == "medium"

        queue_denied = plain.request(
            "thread/queue/add",
            {
                "threadId": thread_id,
                "clientUserMessageId": "proof-1",
                "input": [{"type": "text", "text": "queued"}],
            },
        )
        assert queue_denied["error"]["code"] == -32600
        assert "requires experimentalApi capability" in queue_denied["error"]["message"]
        plain.close()
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
            f"{qualification_codex_binary()} --remote unix://{env.root / 'missing.sock'} "
            f"resume {thread_id}"
        )
        # The TUI must exit instead of silently starting an embedded backend.
        wait_until(lambda: not ui.session_alive(), timeout=60, what="bad-remote TUI to exit")

        # Our backend is untouched and still ours alone.
        response = control.request("thread/list", {})
        assert "result" in response
        control.close()
