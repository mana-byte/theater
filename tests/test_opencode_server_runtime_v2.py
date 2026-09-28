"""The OpenCode 2.x server runtime against a loopback fake answering 2.0.18's `/api` routes."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from test_opencode_server_runtime import PASSWORD, ServerFake

from theater.harness.builtin.plugins.opencode import server_live_v2, server_runtime_v2
from theater.harness.builtin.plugins.opencode.server_plan import SERVER_SECRET_ENV
from theater.harness.builtin.plugins.opencode.server_runtime import (
    opencode_server_runtime_factory,
)
from theater.harness.builtin.plugins.opencode.server_runtime_v2 import OpenCodeServerV2Runtime
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    DeliveryResult,
    RuntimeCapability,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeIO,
    SessionOpenMode,
)


class V2Fake(ServerFake):
    """`/api` routes with `{data}` bodies; a prompt keeps its session active until released."""

    def __init__(self) -> None:
        super().__init__()
        self.active: set[str] = set()

    def bodies(self, suffix: str) -> list[dict[str, Any]]:
        return [json.loads(r["body"]) for r in self.requests if r["path"].endswith(suffix)]

    async def _route(self, record: dict[str, Any], writer, body: bytes) -> None:
        if not self._authorized(record):
            await self._send(writer, 401, "Unauthorized", b"{}")
            return
        method, segments = record["method"], [s for s in record["path"].split("/") if s]
        answer: object = None
        if segments == ["api", "info"]:
            answer = {"version": "2.0.18", "pid": 1, "urls": [], "paths": {"tmp": "/tmp"}}
        elif segments == ["api", "session"] and method == "POST":
            answer = {"data": {"id": self._mint_session()}}
        elif segments == ["api", "session", "active"]:
            answer = {"data": {sid: {} for sid in self.active}}
        elif segments[:2] == ["api", "session"] and segments[2] in self.sessions:
            sid = segments[2]
            if segments[3:] == ["prompt"] and method == "POST":
                self.active.add(sid)
                answer = {"data": {"id": json.loads(body)["id"], "sessionID": sid}}
            elif len(segments) == 3:
                answer = {"data": {"id": sid}}
        if answer is None:
            await self._send(writer, 404, "Not Found", b"{}")
        else:
            await self._send(writer, 200, "OK", json.dumps(answer).encode())


class _IO(RuntimeIO):
    async def connect(self, endpoint: str, *, timeout: float) -> Any:
        raise AssertionError("the server runtime never opens a frontend connection")


@pytest.fixture
async def server():
    fake = V2Fake()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def runtime(server: V2Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_runtime_v2, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(server_live_v2, "_ADMISSION_GRACE_SECONDS", 0.2)
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    token = tmp_path / "runtime.token"
    token.write_text(PASSWORD)
    token.chmod(0o600)
    built = opencode_server_runtime_factory(
        RuntimeContext(
            participant_id="h00000000001",
            cwd="/work",
            io=_IO(),
            backend_generation=1,
            endpoint=server.endpoint,
            token_file=token,
            approval="manual",
            model="anthropic/claude#high",
        )
    )
    assert isinstance(built, OpenCodeServerV2Runtime)
    return built


async def _settles(runtime: OpenCodeServerV2Runtime, state: RuntimeExecutionState) -> None:
    for _ in range(400):
        if (await runtime.snapshot()).execution_state is state:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"execution state never became {state}")


async def test_a_2x_session_takes_prompts_over_http(server: V2Fake, runtime) -> None:
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id == "ses_fake_1"
    assert binding.native_version == "2.0.18"
    assert server.bodies("/api/session") == [
        {
            "location": {"directory": "/work"},
            "model": {"providerID": "anthropic", "id": "claude", "variant": "high"},
        }
    ]
    pane = await runtime.frontend_plan(native_session_id="ses_fake_1")
    assert pane.argv == ["opencode", "--server", server.endpoint, "-s", "ses_fake_1"]
    assert pane.secret_env == {SERVER_SECRET_ENV: runtime.context.token_file}
    assert "OPENCODE_CLI_CONFIG_CONTENT" in pane.env
    capabilities = (await runtime.snapshot()).capabilities
    assert capabilities.available == {RuntimeCapability.SEND, RuntimeCapability.QUEUE_FOLLOWUP}
    assert (
        capabilities.unavailable_reasons[RuntimeCapability.INTERRUPT]
        is CapabilityUnavailableReason.THEATER_POLICY
    )

    sent = await runtime.send(operation_id="op-1", prompt="hello")
    assert sent.result is DeliveryResult.ACCEPTED
    assert server.bodies("/prompt") == [
        {"id": sent.native_turn_id, "text": "hello", "delivery": "queue"}
    ]
    busy = await runtime.send(operation_id="op-2", prompt="again")
    assert (busy.result, busy.error_code) == (DeliveryResult.REJECTED, "busy")

    server.active.clear()
    await _settles(runtime, RuntimeExecutionState.IDLE)
    again = await runtime.send(operation_id="op-3", prompt="again")
    assert again.result is DeliveryResult.ACCEPTED
    await runtime.aclose()


async def test_a_2x_fork_is_refused_before_the_server_is_asked(server: V2Fake, runtime) -> None:
    """The parent lives in another lineage's database; the spawn resumes on the legacy route."""
    with pytest.raises(RuntimeError, match="legacy route"):
        await runtime.open_session(mode=SessionOpenMode.FORK, native_session_id="ses_parent")
    assert server.requests == []
    await runtime.aclose()
