"""Phase 0 proof: the Vibe app-server topology blocker and capability schemas.

Offline conformance pins the failed-attachment record; the live proof is
opt-in via THEATER_VIBE_NATIVE_PROOF=1 and drives the pinned stock release.
Nothing here is production runtime code; the shipped manifest stays legacy.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "vibe_app_server"
PINNED_VERSION = "2.25.1"
PINNED_COMMIT = "2817f3df81ae05d49ba9538262edb1d5a18fa006"
DEFAULT_CHECKOUT = Path("/Users/manaiki.laut/Desktop/coding_clis/mistral-vibe")
NATIVE_PROOF_ENV = "THEATER_VIBE_NATIVE_PROOF"
CHECKOUT_ENV = "THEATER_VIBE_CHECKOUT"
REQUEST_DEADLINE_SECONDS = 20.0
PROCESS_STOP_SECONDS = 8.0

CAPABILITY_MAP_METHODS = (
    "session/start",
    "session/resume",
    "session/continue",
    "session/read",
    "turn/start",
    "turn/steer",
    "turn/interrupt",
    "session/turn/enqueue",
    "session/turn/queue/read",
    "session/turn/queue/remove",
    "session/turn/queue/replace",
    "session/turn/queue/steer",
    "session/settings/update",
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Offline conformance — the pinned failed proof and its schemas
# ---------------------------------------------------------------------------


def test_failed_proof_is_pinned_to_the_inspected_release() -> None:
    release = _fixture("installed_release.json")
    assert release["version"] == PINNED_VERSION
    assert release["commit"] == PINNED_COMMIT
    topology = _fixture("topology.json")
    assert topology["proofPassed"] is False
    assert topology["goCriteriaStatus"] == {
        "observerControlRole": False,
        "stockTuiExtension": False,
        "sharedBroker": False,
    }
    assert "one attached root runtime" in topology["upstreamAnchor"]


def test_method_catalogue_covers_the_plan_capability_map() -> None:
    methods = set(_fixture("capability_schemas.json")["serverMethods"])
    missing = [method for method in CAPABILITY_MAP_METHODS if method not in methods]
    assert not missing, f"capability-map methods absent from the pinned catalogue: {missing}"


def test_exact_turn_guards_are_pinned_in_the_schemas() -> None:
    schemas = _fixture("capability_schemas.json")
    guard = schemas["capabilitySchemas"]
    assert guard["turnSteer"]["expectedTurnId"] == {"required": True}
    assert guard["turnInterrupt"]["expectedTurnId"] == {"required": True}
    assert guard["queueSteer"]["expectedTurnId"] == {"required": True}
    assert guard["turnStart"].get("expectedTurnId") is None
    codes = set(schemas["errorCodes"])
    assert {"stale_turn", "not_steerable", "conflict", "not_found"} <= codes
    assert guard["publicTurnStatus"] == ["in_progress", "completed", "failed", "interrupted"]


def test_receipt_and_settings_schemas_for_a_future_adapter() -> None:
    guard = _fixture("capability_schemas.json")["capabilitySchemas"]
    assert guard["turnStart"]["idempotencyKey"]["required"] is False
    assert guard["turnStart"]["clientUserMessageId"]["required"] is False
    assert guard["enqueueResponse"] == {"queueItemId": {"required": True}}
    assert guard["queueRemove"] == {
        "sessionId": {"required": True},
        "queueItemId": {"required": True},
    }
    # Verified fields only: settings update carries no model or effort field.
    assert set(guard["settingsUpdate"]) == {"sessionId", "maxTurns", "maxTokens"}


def test_handshake_wire_shape_is_pinned() -> None:
    handshake = _fixture("handshake.json")
    assert handshake["wireFacts"] == {
        "aliasStyle": "camelCase",
        "notificationParamsRequired": True,
        "initializeOnceOnly": True,
        "initializeReturnsServerInfoOnly": True,
    }
    assert handshake["initializeResponse"]["result"]["serverInfo"]["version"] == PINNED_VERSION


def test_topology_observations_pin_the_blocker() -> None:
    observed = _fixture("topology.json")["observed"]
    assert observed["crossProcessResumeOfLiveSession"]["harnessCode"] == "session_busy"
    second_start = observed["secondSessionStartSameConnection"]
    assert second_start["code"] == "conflict"
    assert second_start["message"] == "A session is already attached"
    assert observed["activeTurnSteerWithBogusExpectedTurnId"]["code"] == "stale_turn"
    assert observed["idleSteerWithBogusExpectedTurnId"]["code"] == "conflict"
    assert observed["queueSteer"]["code"] == "not_implemented"
    assert observed["turnStartWrongSessionId"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# Live topology proof — opt-in, drives the pinned stock release
# ---------------------------------------------------------------------------


def _pinned_server_executable() -> Path | None:
    root = Path(os.environ.get(CHECKOUT_ENV, str(DEFAULT_CHECKOUT)))
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return None
    # Version drift must be resolved by re-capturing fixtures, never by re-running.
    with pyproject.open("rb") as handle:
        version = tomllib.load(handle)["project"]["version"]
    if version != PINNED_VERSION:
        return None
    executable = root / ".venv" / "bin" / "vibe-app-server"
    if not executable.is_file():
        return None
    return executable


@pytest.fixture
def server_executable() -> Path:
    if os.environ.get(NATIVE_PROOF_ENV) != "1":
        pytest.skip(f"set {NATIVE_PROOF_ENV}=1 to run the live Vibe topology proof")
    executable = _pinned_server_executable()
    if executable is None:
        pytest.skip(
            "pinned mistral-vibe checkout absent or version drifted "
            f"(set {CHECKOUT_ENV}); re-capture tests/fixtures/vibe_app_server"
        )
    return executable


class _AppServer:
    """One stdio connection to one app-server process, deadlines on every frame."""

    def __init__(self, executable: Path, home: Path) -> None:
        self._executable = executable
        self._home = home
        self._process: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self.server_info: dict | None = None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            str(self._executable),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=2**24,
            cwd=str(self._home),
            env={**os.environ, "VIBE_HOME": str(self._home)},
        )
        initialize = await self.request(
            "initialize",
            {
                "clientInfo": {"name": "theater-proof", "version": "0"},
                "capabilities": {
                    "callbackKinds": ["approval", "user_input"],
                    "clientTools": [],
                },
            },
        )
        self.server_info = initialize["result"]["serverInfo"]
        await self.notify("initialized", {})

    async def request(self, method: str, params: dict | None = None) -> dict:
        self._next_id += 1
        frame = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            frame["params"] = params
        return await self._roundtrip(frame, self._next_id)

    async def notify(self, method: str, params: dict) -> None:
        stream = self._writer()
        stream.write(
            (json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n").encode()
        )
        await stream.drain()

    async def _roundtrip(self, frame: dict, request_id: int) -> dict:
        stream = self._writer()
        stream.write((json.dumps(frame) + "\n").encode())
        await stream.drain()
        while True:
            line = await asyncio.wait_for(self._readline(), REQUEST_DEADLINE_SECONDS)
            message = json.loads(line)
            if message.get("id") == request_id:
                return message

    def _writer(self) -> asyncio.StreamWriter:
        # Callers re-assert per call: an await can race a stop().
        process = self._process
        if not isinstance(process, asyncio.subprocess.Process):
            raise TypeError("app server is not started")
        stdin = process.stdin
        if not isinstance(stdin, asyncio.StreamWriter):
            raise TypeError("app server stdin is not a pipe")
        return stdin

    async def _readline(self) -> bytes:
        process = self._process
        if not isinstance(process, asyncio.subprocess.Process):
            raise TypeError("app server is not started")
        stdout = process.stdout
        if not isinstance(stdout, asyncio.StreamReader):
            raise TypeError("app server stdout is not a pipe")
        line = await stdout.readline()
        if not line:
            raise RuntimeError("app server closed its stdio stream")
        return line

    async def stop(self) -> None:
        if self._process is None:
            return
        process = self._process
        if process.returncode is None:
            if isinstance(process.stdin, asyncio.StreamWriter):
                process.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), PROCESS_STOP_SECONDS)
            if process.returncode is None:
                process.kill()
                await process.wait()


@contextlib.asynccontextmanager
async def _server(executable: Path, home: Path) -> AsyncIterator[_AppServer]:
    home.mkdir(parents=True, exist_ok=True)
    server = _AppServer(executable, home)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def _wire_error(message: dict) -> dict:
    assert "error" in message, f"expected a typed error, got: {message}"
    return message["error"]


async def test_live_handshake_guards_and_receipts(server_executable: Path, tmp_path: Path) -> None:
    async with _server(server_executable, tmp_path / "home") as server:
        assert server.server_info == {"name": "vibe-app-server", "version": PINNED_VERSION}
        passive = await server.request("session/list", {"limit": 50})
        assert passive["result"]["items"] == []
        session_id = (await server.request("session/start", {}))["result"]["state"]["session"]["id"]
        steer = _wire_error(
            await server.request(
                "turn/steer",
                {
                    "sessionId": session_id,
                    "expectedTurnId": "turn-bogus",
                    "message": [{"type": "text", "text": "x"}],
                },
            )
        )
        assert (steer["code"], steer["message"]) == ("conflict", "No active turn")
        interrupt = _wire_error(
            await server.request(
                "turn/interrupt",
                {"sessionId": session_id, "expectedTurnId": "turn-bogus"},
            )
        )
        assert (interrupt["code"], interrupt["message"]) == ("conflict", "No active turn")
        wrong_session = _wire_error(
            await server.request(
                "turn/start",
                {"sessionId": "session-bogus", "message": [{"type": "text", "text": "x"}]},
            )
        )
        assert wrong_session["code"] == "not_found"
        settings = _wire_error(
            await server.request(
                "session/settings/update",
                {"sessionId": session_id, "update": {"bogusField": 1}},
            )
        )
        assert settings["code"] == "invalid_params"
        enqueued = await server.request(
            "session/turn/enqueue",
            {
                "sessionId": session_id,
                "idempotencyKey": "op-1",
                "entries": [{"role": "user", "content": [{"type": "text", "text": "queued"}]}],
            },
        )
        queue_item_id = enqueued["result"]["queueItemId"]
        removed = await server.request(
            "session/turn/queue/remove",
            {"sessionId": session_id, "queueItemId": queue_item_id},
        )
        assert "result" in removed
        duplicate = _wire_error(
            await server.request(
                "session/turn/enqueue",
                {
                    "sessionId": session_id,
                    "idempotencyKey": "op-1",
                    "entries": [
                        {"role": "user", "content": [{"type": "text", "text": "different"}]}
                    ],
                },
            )
        )
        assert duplicate["code"] == "conflict"
        queue_steer = _wire_error(
            await server.request(
                "session/turn/queue/steer",
                {
                    "sessionId": session_id,
                    "queueItemId": queue_item_id,
                    "expectedTurnId": "turn-bogus",
                },
            )
        )
        assert queue_steer["code"] == "not_implemented"


async def test_live_attachment_is_single_client(server_executable: Path, tmp_path: Path) -> None:
    home = tmp_path / "home"
    async with _server(server_executable, home) as first:
        session_id = (await first.request("session/start", {}))["result"]["state"]["session"]["id"]
        # A second process cannot attach the live session: the recorded blocker.
        async with _server(server_executable, home) as second:
            resume = _wire_error(await second.request("session/resume", {"sessionId": session_id}))
            assert resume["code"] == "conflict"
            assert resume["data"]["harnessCode"] == "session_busy"
        # The stock backend refuses a second attach even on this connection.
        second_start = _wire_error(await first.request("session/start", {}))
        assert second_start["code"] == "conflict"
        assert second_start["message"] == "A session is already attached"


async def test_live_turn_receipt_and_stale_turn_guard(
    server_executable: Path, tmp_path: Path
) -> None:
    async with _server(server_executable, tmp_path / "home") as server:
        session_id = (await server.request("session/start", {}))["result"]["state"]["session"]["id"]
        admitted = await server.request(
            "turn/start",
            {
                "sessionId": session_id,
                "idempotencyKey": "op-turn-1",
                "message": [{"type": "text", "text": "proof"}],
            },
        )
        turn = admitted["result"]["turn"]
        assert turn["status"] == "in_progress"
        assert turn["sessionId"] == session_id
        stale = _wire_error(
            await server.request(
                "turn/steer",
                {
                    "sessionId": session_id,
                    "expectedTurnId": "turn-bogus",
                    "message": [{"type": "text", "text": "x"}],
                },
            )
        )
        assert stale["code"] == "stale_turn"
        assert stale["data"]["activeTurnId"] == turn["id"]
