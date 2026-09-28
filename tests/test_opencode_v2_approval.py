"""The fail-closed 2.x approval gate against a loopback fake answering `/api/plugin`.

The fake records every request, so the tests assert behaviour: what was polled and
what refused. The stock-2.0.18 regression runs only when `THEATER_OPENCODE_STOCK_PROBE`
names the stock binary; it needs no model and changes no user state.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
from pathlib import Path
from typing import Any

import pytest
from test_opencode_server_runtime import PASSWORD, ServerFake

from theater.harness.builtin.plugins.opencode.approval_v2 import (
    PluginNotActive,
    classify_plugin_state,
    require_plugin_active,
)
from theater.harness.builtin.plugins.opencode.http import OpenCodeClient
from theater.harness.builtin.plugins.opencode.http_v2 import OpenCodeV2Client
from theater.harness.builtin.plugins.opencode.native_plugin_v2 import (
    PLUGIN_ID,
    plugin_dir,
    render_native_plugin_v2,
)
from theater.harness.builtin.plugins.opencode.server_discovery import (
    parse_server_stdout_endpoint,
)

SOURCE = "/work/x.opencode/server.js"


class PluginFake(ServerFake):
    """`/api/plugin` with a scripted entry list; records every request."""

    def __init__(self) -> None:
        super().__init__()
        self.plugins: list[dict[str, Any]] = []
        self.plugin_after = 0

    async def _route(self, record: dict[str, Any], writer, body: bytes) -> None:
        if not self._authorized(record):
            await self._send(writer, 401, "Unauthorized", b"{}")
            return
        if [s for s in record["path"].split("/") if s] == ["api", "plugin"]:
            seen = len([r for r in self.requests if r["path"].startswith("/api/plugin")])
            if not self.plugins and self.plugin_after and seen > self.plugin_after:
                answer: object = {"data": [self._active_entry()]}
            else:
                answer = {"data": list(self.plugins)}
            await self._send(writer, 200, "OK", json.dumps(answer).encode())
            return
        await self._send(writer, 404, "Not Found", b"{}")

    def _active_entry(self) -> dict[str, Any]:
        return active_entry()

    def plugin_requests(self) -> int:
        return len([r for r in self.requests if r["path"].startswith("/api/plugin")])


def active_entry(**overrides: object) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": PLUGIN_ID,
        "source": {"type": "local", "path": SOURCE},
        "state": {"status": "active"},
    }
    entry.update(overrides)
    return entry


@pytest.fixture
async def fake():
    server = PluginFake()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def client(fake: PluginFake, tmp_path: Path) -> OpenCodeV2Client:
    token = tmp_path / "credential"
    token.write_text(PASSWORD)
    token.chmod(0o600)
    return OpenCodeV2Client(endpoint=fake.endpoint, token_file=token)


# ---- the plugin gate -----------------------------------------------------


def test_a_missing_plugin_refuses():
    with pytest.raises(PluginNotActive, match="not loaded"):
        classify_plugin_state([], plugin_id=PLUGIN_ID, source_path=SOURCE)


def test_a_failed_plugin_refuses_with_its_error():
    entry = active_entry(state={"status": "failed", "error": "boom"})
    with pytest.raises(PluginNotActive, match="boom"):
        classify_plugin_state([entry], plugin_id=PLUGIN_ID, source_path=SOURCE)


def test_an_ambiguous_plugin_refuses():
    with pytest.raises(PluginNotActive, match="2 times"):
        classify_plugin_state(
            [active_entry(), active_entry()], plugin_id=PLUGIN_ID, source_path=SOURCE
        )


def test_a_foreign_plugin_path_refuses():
    entry = active_entry(source={"type": "local", "path": "/elsewhere/server.js"})
    with pytest.raises(PluginNotActive, match="not the generated"):
        classify_plugin_state([entry], plugin_id=PLUGIN_ID, source_path=SOURCE)


def test_a_nonlocal_source_refuses():
    entry = active_entry(source={"type": "npm", "path": SOURCE})
    with pytest.raises(PluginNotActive, match="not the launch-local"):
        classify_plugin_state([entry], plugin_id=PLUGIN_ID, source_path=SOURCE)


async def test_a_missing_plugin_is_polled_until_it_registers(fake, client):
    fake.plugin_after = 2
    await require_plugin_active(client, plugin_id=PLUGIN_ID, source_path=SOURCE, interval=0.01)
    assert fake.plugin_requests() >= 3


async def test_a_reported_plugin_failure_refuses_immediately(fake, client):
    fake.plugins = [active_entry(state={"status": "failed", "error": "boom"})]
    with pytest.raises(PluginNotActive, match="boom"):
        await require_plugin_active(client, plugin_id=PLUGIN_ID, source_path=SOURCE)
    assert fake.plugin_requests() == 1


async def test_a_plugin_that_never_registers_refuses_at_the_deadline(fake, client):
    with pytest.raises(PluginNotActive, match="not loaded"):
        await require_plugin_active(
            client, plugin_id=PLUGIN_ID, source_path=SOURCE, timeout=0.05, interval=0.01
        )


# ---- the stock regression (opt-in) ----------------------------------------


async def _evaluate(transport: OpenCodeClient, sid: str, action: str) -> str:
    result = await transport._json_request(
        "POST",
        f"/api/session/{sid}/permission",
        body={"agent": "build", "action": action, "resources": ["probe"]},
    )
    effect = result["data"]["effect"]
    if effect == "ask":
        await transport._json_request(
            "POST",
            f"/api/session/{sid}/permission/{result['data']['id']}/reply",
            body={"decision": "reject"},
        )
    return effect


@pytest.mark.skipif(
    not os.environ.get("THEATER_OPENCODE_STOCK_PROBE"), reason="opt-in stock 2.0.18 probe"
)
@pytest.mark.parametrize("approval", ["manual", "edits"])
async def test_stock_2_0_18_keeps_native_denies_and_tightens_saved_allows(tmp_path, approval):
    """Against the stock binary with an agent-level edit deny: the deny survives,
    native allows the hook tightens still ask, and a saved always-allow cannot
    bypass the enforced policy."""
    stock = os.environ["THEATER_OPENCODE_STOCK_PROBE"]
    token = tmp_path / "credential"
    password = secrets.token_urlsafe(32)
    token.write_text(password)
    token.chmod(0o600)
    config = tmp_path / "config.json"
    files = render_native_plugin_v2("stock", config, token, approval)
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    config.write_text(
        json.dumps(
            {
                "plugin": [plugin_dir(config).resolve().as_uri()],
                "agents": {
                    "build": {
                        "permissions": [{"action": "edit", "resource": "*", "effect": "deny"}]
                    }
                },
            }
        )
    )
    env = {
        **os.environ,
        "OPENCODE_CONFIG": str(config),
        "OPENCODE_CONFIG_PROJECT_DISABLE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_FILEWATCHER_DISABLE": "1",
        "OPENCODE_DB": str(tmp_path / "opencode-v2.db"),
        "OPENCODE_SERVER_PASSWORD": password,
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
    }
    process = await asyncio.create_subprocess_exec(
        stock,
        "serve",
        "--hostname",
        "127.0.0.1",
        "--port",
        "0",
        cwd=tmp_path,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(60):
            while line := await process.stdout.readline():
                endpoint = parse_server_stdout_endpoint(line.decode())
                if endpoint:
                    break
            else:
                raise AssertionError("the stock server never announced its endpoint")
            transport = OpenCodeClient(endpoint=endpoint, token_file=token)
            client = OpenCodeV2Client(endpoint=endpoint, token_file=token)
            sid = await client.create_session(directory=os.path.realpath(tmp_path))
            source = str((plugin_dir(config) / "server.js").resolve())
            await require_plugin_active(client, plugin_id=PLUGIN_ID, source_path=source)
            # The agent's edit deny is never overridden, in either approval mode.
            assert await _evaluate(transport, sid, "edit") == "deny"
            assert await _evaluate(transport, sid, "shell") == "ask"
            assert await _evaluate(transport, sid, "read") == "allow"
            assert await _evaluate(transport, sid, "provider.use") == "allow"
            # A saved always-allow still cannot bypass the enforced policy.
            result = await transport._json_request(
                "POST",
                f"/api/session/{sid}/permission",
                body={
                    "agent": "build",
                    "action": "write",
                    "resources": ["probe"],
                    "save": ["always"],
                },
            )
            assert result["data"]["effect"] == "ask"
            await transport._json_request(
                "POST",
                f"/api/session/{sid}/permission/{result['data']['id']}/reply",
                body={"decision": "always"},
            )
            assert await _evaluate(transport, sid, "write") == "ask"
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()
