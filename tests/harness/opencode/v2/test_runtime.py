"""The OpenCode 2.x server runtime: prompts over HTTP and plugin proofs."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.support.opencode_releases import V2_RELEASE
from tests.support.opencode_server import PASSWORD, ServerFake
from theater import paths
from theater.harness.builtin.plugins.opencode import server_live_v2, server_runtime_v2
from theater.harness.builtin.plugins.opencode.approval_v2 import PluginNotActive
from theater.harness.builtin.plugins.opencode.dialect import v2_lineage_marker
from theater.harness.builtin.plugins.opencode.http import OpenCodeHttpError
from theater.harness.builtin.plugins.opencode.native_plugin_v2 import PLUGIN_ID, plugin_dir
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
        self.plugin_state = "active"
        self.session_directory = "/work"
        self.failed_directory: str | None = None
        #: The server clock's stamp on an admitted prompt, and on the session's last idle.
        self.prompt_created: int | None = 1_000
        self.idle_at: int | None = None

    def bodies(self, suffix: str) -> list[dict[str, Any]]:
        return [json.loads(r["body"]) for r in self.requests if r["path"].endswith(suffix)]

    def end_turn(self, *, idle_at: int | None) -> None:
        """The session leaves `active`; a turn that ran to its end also gets an idle stamp."""
        self.active.clear()
        self.idle_at = idle_at

    def _plugin_entries(self) -> list[dict[str, Any]]:
        if self.plugin_state == "missing":
            return []
        source = str((plugin_dir(paths.mcp_config_path("h00000000001")) / "server.js").resolve())
        state: dict[str, Any] = (
            {"status": "failed", "error": "boom"}
            if self.plugin_state == "failed"
            else {"status": "active"}
        )
        return [{"id": PLUGIN_ID, "source": {"type": "local", "path": source}, "state": state}]

    def _session_answer(
        self, sid: str, rest: list[str], method: str, body: bytes
    ) -> dict[str, Any] | None:
        if rest == ["prompt"] and method == "POST":
            self.active.add(sid)
            admitted: dict[str, Any] = {"id": json.loads(body)["id"], "sessionID": sid}
            if self.prompt_created is not None:
                admitted["time"] = {"created": self.prompt_created}
            return {"data": admitted}
        if rest:
            return None
        times = {"created": 1, "updated": 1}
        if self.idle_at is not None:
            times["idle"] = self.idle_at
        location = {"directory": self.session_directory}
        return {"data": {"id": sid, "location": location, "time": times}}

    async def _route(self, record: dict[str, Any], writer, body: bytes) -> None:
        if not self._authorized(record):
            await self._send(writer, 401, "Unauthorized", b"{}")
            return
        method, segments = (
            record["method"],
            [s for s in record["path"].split("?")[0].split("/") if s],
        )
        answer: object = None
        if segments == ["api", "info"]:
            answer = {"version": V2_RELEASE, "pid": 1, "urls": [], "paths": {"tmp": "/tmp"}}
        elif segments == ["api", "plugin"]:
            from urllib.parse import parse_qs, urlsplit

            directory = parse_qs(urlsplit(record["path"]).query).get("location[directory]", [""])[0]
            if directory == self.failed_directory:
                self.plugin_state = "failed"
            answer = {"data": self._plugin_entries()}
        elif segments == ["api", "session"] and method == "POST":
            answer = {"data": {"id": self._mint_session()}}
        elif segments == ["api", "session", "active"]:
            answer = {"data": {sid: {} for sid in self.active}}
        elif segments[:2] == ["api", "session"] and segments[2] in self.sessions:
            answer = self._session_answer(segments[2], segments[3:], method, body)
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
def fake_opencode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The frontend plan pins the resolved binary; planning needs it to exist."""
    fake = tmp_path / "bin" / "opencode"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    return fake


@pytest.fixture
def runtime(server: V2Fake, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_opencode):
    monkeypatch.setattr(server_runtime_v2, "_POLL_SECONDS", 0.01)
    monkeypatch.setattr(server_live_v2, "_ADMISSION_GRACE_SECONDS", 0.2)
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", V2_RELEASE)
    token = tmp_path / "runtime.token"
    token.write_text(PASSWORD)
    token.chmod(0o600)
    # The persisted lineage marker names this participant as a native 2.x spawn.
    marker, marker_text = v2_lineage_marker("h00000000001")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(marker_text)
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


async def test_a_2x_session_takes_prompts_over_http(server: V2Fake, runtime, fake_opencode) -> None:
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id == "ses_fake_1"
    assert binding.native_version == V2_RELEASE
    assert server.bodies("/api/session") == [
        {
            "location": {"directory": "/work"},
            "model": {"providerID": "anthropic", "id": "claude", "variant": "high"},
        }
    ]
    pane = await runtime.frontend_plan(native_session_id="ses_fake_1")
    assert pane.argv == [
        str(fake_opencode.resolve()),
        "--server",
        server.endpoint,
        "-s",
        "ses_fake_1",
    ]
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


@pytest.fixture
def unpolled(runtime, monkeypatch: pytest.MonkeyPatch) -> None:
    """No poll loop and no lapsing grace: only reads a call makes itself can see the server."""
    monkeypatch.setattr(OpenCodeServerV2Runtime, "_start_polling", lambda self: None)
    monkeypatch.setattr(server_live_v2, "_ADMISSION_GRACE_SECONDS", 3600.0)


async def test_a_turn_that_ended_before_any_poll_saw_it_leaves_the_session_idle(
    server: V2Fake, runtime, unpolled
) -> None:
    await runtime.open_session(mode=SessionOpenMode.NEW)
    assert (await runtime.send(operation_id="op-1", prompt="hello")).result is (
        DeliveryResult.ACCEPTED
    )

    # The whole turn fit between two polls: the server stamped idle after the admission.
    server.end_turn(idle_at=server.prompt_created + 50)

    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    again = await runtime.send(operation_id="op-2", prompt="again")
    assert again.result is DeliveryResult.ACCEPTED
    await runtime.aclose()


@pytest.mark.parametrize(
    ("prompt_created", "idle_at"),
    [
        pytest.param(1_000, None, id="no_idle_stamp_yet"),
        pytest.param(1_000, 999, id="idle_before_the_admission"),
        pytest.param(1_000, 1_000, id="idle_at_the_admission"),
        pytest.param(None, 5_000, id="admission_carries_no_time"),
    ],
)
async def test_an_admitted_prompt_stays_busy_until_the_server_proves_its_turn_ended(
    server: V2Fake, runtime, unpolled, prompt_created: int | None, idle_at: int | None
) -> None:
    server.prompt_created = prompt_created
    await runtime.open_session(mode=SessionOpenMode.NEW)
    assert (await runtime.send(operation_id="op-1", prompt="hello")).result is (
        DeliveryResult.ACCEPTED
    )
    server.end_turn(idle_at=idle_at)

    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
    refused = await runtime.send(operation_id="op-2", prompt="again")
    assert (refused.result, refused.error_code) == (DeliveryResult.REJECTED, "busy")
    assert len(server.bodies("/prompt")) == 1
    await runtime.aclose()


async def test_a_snapshot_reads_the_server_rather_than_a_poll_that_may_predate_the_turn_end(
    server: V2Fake, runtime, unpolled
) -> None:
    await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.send(operation_id="op-1", prompt="hello")
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE

    # An interrupt ends the turn at once; no poll has run since the server last listed it.
    server.end_turn(idle_at=server.prompt_created + 50)

    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    assert (await runtime.send(operation_id="op-2", prompt="again")).result is (
        DeliveryResult.ACCEPTED
    )
    await runtime.aclose()


async def test_a_session_the_server_still_lists_is_busy_however_old_the_last_poll(
    server: V2Fake, runtime, unpolled
) -> None:
    await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.send(operation_id="op-1", prompt="hello")

    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
    refused = await runtime.send(operation_id="op-2", prompt="again")

    assert (refused.result, refused.error_code) == (DeliveryResult.REJECTED, "busy")
    assert len(server.bodies("/prompt")) == 1
    await runtime.aclose()


@pytest.mark.parametrize(
    ("written", "status", "result", "code"),
    [
        (False, 500, DeliveryResult.REJECTED, "server_refused"),
        (True, 400, DeliveryResult.REJECTED, "server_refused"),
        (True, 404, DeliveryResult.REJECTED, "invalid_session"),
        (True, 500, DeliveryResult.UNKNOWN, "delivery_unknown"),
    ],
)
async def test_v2_prompt_http_error_outcomes(
    runtime, monkeypatch, written: bool, status: int, result: DeliveryResult, code: str
) -> None:
    await runtime.open_session(mode=SessionOpenMode.NEW)
    error = OpenCodeHttpError(
        "POST", "/api/session/ses_fake_1/prompt", "failure", status=status, written=written
    )

    async def fail_prompt(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(runtime._client, "prompt", fail_prompt)
    receipt = await runtime.send(operation_id="op-http", prompt="hello")
    assert (receipt.result, receipt.error_code) == (result, code)
    if not written:
        assert receipt.error == f"the OpenCode server refused the prompt before delivery ({error})"
    elif 400 <= status < 500:
        assert (
            receipt.error == f"the OpenCode server refused the prompt with HTTP {status} (failure)"
        )
    else:
        assert receipt.error == (
            f"the OpenCode delivery became uncertain and Theater did not replay it ({error})"
        )
    await runtime.aclose()


async def test_a_2x_fork_is_refused_before_the_server_is_asked(server: V2Fake, runtime) -> None:
    """The parent lives in another lineage's database; the spawn resumes on the legacy route."""
    with pytest.raises(RuntimeError, match="legacy route"):
        await runtime.open_session(mode=SessionOpenMode.FORK, native_session_id="ses_parent")
    assert server.requests == []
    await runtime.aclose()


async def test_a_failed_plugin_refuses_the_session_before_any_prompt(
    server: V2Fake, runtime
) -> None:
    """Core swallows a plugin load failure; an unproven hook means no send access."""
    server.plugin_state = "failed"
    with pytest.raises(PluginNotActive, match="did not load"):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    assert server.bodies("/prompt") == []
    await runtime.aclose()


async def test_reconnect_proves_the_plugin_at_the_persisted_session_location(server, runtime):
    sid = server._mint_session()
    server.session_directory = "/moved"
    server.failed_directory = "/moved"
    with pytest.raises(PluginNotActive, match="did not load"):
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id=sid)
    assert server.bodies("/prompt") == []
    await runtime.aclose()
