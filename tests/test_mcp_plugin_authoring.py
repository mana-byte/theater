"""Behavioral coverage for the two documented MCP-server plugin authoring modes."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from theater import config
from theater.mcp_plugins import registry

FIXTURES = Path(__file__).parent / "fixtures" / "mcp_plugins"


@pytest.fixture(autouse=True)
def clean_registry():
    registry.MCP_SERVERS.clear()
    registry._PLUGINS.clear()
    registry._DIAGNOSTICS.clear()
    yield
    registry.MCP_SERVERS.clear()
    registry._PLUGINS.clear()
    registry._DIAGNOSTICS.clear()


async def _tool_call(
    plan, *, name: str, arguments: dict, env: dict[str, str]
) -> tuple[set[str], object]:
    parameters = StdioServerParameters(
        command=plan.command,
        args=list(plan.argv),
        env={**plan.env, **env},
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        result = await session.call_tool(name, arguments)
    payload = (
        result.structured_content
        if result.structured_content is not None
        else json.loads(result.content[0].text)
    )
    return {tool.name for tool in tools.tools}, payload


async def test_native_fixture_launches_a_stdio_mcp_server_and_uses_typed_client(tmp_path):
    settings = config.Config(
        mcp=config.McpSection(enabled=["native"], plugins={"native": {"label": "ready"}})
    )
    assert registry.install(settings, local_dir=FIXTURES, shipped_dir=tmp_path / "shipped") == [
        "native"
    ]
    credential = tmp_path / "credential"
    credential.write_text("fixture-credential\n", encoding="utf-8")
    plan = registry.get("native").plan_launch(participant_id="p-native", cwd=tmp_path)
    assert plan.env == {"FIXTURE_NATIVE_LABEL": "ready"}

    tools, payload = await _tool_call(
        plan,
        name="list_fixture_participants",
        arguments={},
        env={
            "THEATER_PLUGIN_CREDENTIAL_PATH": str(credential),
            "THEATER_PLUGIN_FIXTURE_RESPONSE": '[{"id":"p-native"}]',
        },
    )
    assert tools == {"list_fixture_participants"}
    assert payload == {"result": [{"id": "p-native"}]}


async def test_compat_fixture_launches_a_stdio_mcp_server_through_the_json_gateway(tmp_path):
    settings = config.Config(
        mcp=config.McpSection(
            enabled=["compat"], plugins={"compat": {"endpoint": "https://api.invalid"}}
        )
    )
    assert registry.install(settings, local_dir=FIXTURES, shipped_dir=tmp_path / "shipped") == [
        "compat"
    ]
    gateway_dir = tmp_path / "bin"
    gateway_dir.mkdir()
    gateway = gateway_dir / "theater"
    gateway.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} -m theater.cli "$@"\n',
        encoding="utf-8",
    )
    gateway.chmod(0o700)
    credential = tmp_path / "credential"
    credential.write_text("fixture-credential\n", encoding="utf-8")
    plan = registry.get("compat").plan_launch(participant_id="p-compat", cwd=tmp_path)
    assert plan.env == {"FIXTURE_ENDPOINT": "https://api.invalid"}

    theater_home = Path(tempfile.mkdtemp(prefix="theater-mcp-"))
    received: dict = {}

    async def daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        received.update(request)
        writer.write(
            json.dumps({"id": request["id"], "ok": True, "result": {"id": "p-compat"}}).encode()
            + b"\n"
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(daemon, path=theater_home / "daemon.sock")
    try:
        tools, payload = await _tool_call(
            plan,
            name="get_fixture_participant",
            arguments={"participant_id": "p-compat"},
            env={
                "THEATER_HOME": str(theater_home),
                "THEATER_PLUGIN_CREDENTIAL_PATH": str(credential),
                "PATH": f"{gateway_dir}{os.pathsep}{os.environ['PATH']}",
            },
        )
    finally:
        server.close()
        await server.wait_closed()
        shutil.rmtree(theater_home)

    assert tools == {"get_fixture_participant"}
    assert payload == {"id": "p-compat", "source": "existing-server"}
    assert received["method"] == "plugin.call"
    assert received["params"] == {
        "credential": "fixture-credential",
        "operation": "participants.get",
        "params": {"id": "p-compat"},
    }
