"""Behavioral coverage for the two documented MCP-server plugin authoring modes."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def _module(plugin) -> object:
    return sys.modules[plugin.launch.planner.__module__]


def test_native_fixture_discovers_configures_launches_and_uses_typed_client(tmp_path, monkeypatch):
    settings = config.Config(
        mcp=config.McpSection(enabled=["native"], plugins={"native": {"label": "ready"}})
    )
    assert registry.install(settings, local_dir=FIXTURES, shipped_dir=tmp_path / "shipped") == [
        "native"
    ]
    plugin = registry.get("native")
    plan = plugin.plan_launch(participant_id="p-native", cwd=tmp_path)
    assert plan.command == sys.executable
    assert plan.argv[-1] == "ready"

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

        async def list_participants(self):
            return [{"id": "p-native"}]

    module = _module(plugin)
    monkeypatch.setattr(module, "TheaterPluginClient", Client)
    assert asyncio.run(module.participants()) == [{"id": "p-native"}]


def test_compat_fixture_configures_launch_and_uses_json_gateway(tmp_path, monkeypatch):
    settings = config.Config(
        mcp=config.McpSection(
            enabled=["compat"], plugins={"compat": {"endpoint": "https://api.invalid"}}
        )
    )
    assert registry.install(settings, local_dir=FIXTURES, shipped_dir=tmp_path / "shipped") == [
        "compat"
    ]
    plugin = registry.get("compat")
    assert plugin.plan_launch(participant_id="p-compat", cwd=tmp_path).env == {
        "FIXTURE_ENDPOINT": "https://api.invalid"
    }

    observed = {}

    def run(command, **kwargs):
        observed["command"] = command
        observed["payload"] = json.loads(kwargs["input"])
        return SimpleNamespace(stdout='{"ok":true,"result":{"id":"p-compat"}}')

    module = _module(plugin)
    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.participant("p-compat", credential_file="/tmp/credential") == {"id": "p-compat"}
    assert observed == {
        "command": [
            "theater",
            "plugin",
            "call",
            "participants.get",
            "--credential-file",
            "/tmp/credential",
        ],
        "payload": {"id": "p-compat"},
    }
