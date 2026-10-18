"""Static Beads manifest and play package checks."""

from __future__ import annotations

import asyncio
from pathlib import Path

from theater.mcp_plugins import McpLaunchContext, PluginCapability, validate_manifest
from theater.mcp_plugins.builtin.beads.manifest import MANIFEST, plan
from theater.mcp_plugins.builtin.beads.server import build


def test_beads_manifest_and_static_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    validate_manifest("beads", MANIFEST)
    assert MANIFEST.capabilities == frozenset({PluginCapability.PLAYS_READ})
    assert MANIFEST.plays == ("hierarchy",)
    assert MANIFEST.play_permissions == frozenset({"read", "write"})
    launch = plan(
        McpLaunchContext(
            participant_id="caller",
            cwd=tmp_path,
            state_dir=tmp_path / "state",
            config={"board_ttl_seconds": 0, "bd_path": "/tmp/bd"},
        )
    )
    assert launch.argv[:2] == ("-m", "theater.mcp_plugins.builtin.beads.server")
    assert launch.argv[-2:] == ("--bd-path", "/tmp/bd")
    tools = asyncio.run(build(tmp_path).list_tools())
    assert {tool.name for tool in tools} == {
        "list_tasks",
        "get_task",
        "create_task",
        "update_task",
        "set_dependency",
        "claim_task",
        "close_task",
    }
    for tool in tools:
        assert not {"participant_id", "play_instance_id", "board_dir", "database"} & set(
            tool.input_schema["properties"]
        )
    assert not (tmp_path / "plays").exists()
