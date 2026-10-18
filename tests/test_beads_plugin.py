"""Static Beads manifest and play package checks."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from theater.config import Config
from theater.mcp_plugins import McpLaunchContext, PluginCapability, validate_manifest
from theater.mcp_plugins import registry as mcp_registry
from theater.mcp_plugins.builtin.beads.manifest import MANIFEST, plan
from theater.mcp_plugins.builtin.beads.server import build
from theater.skills import discover


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


def test_shipped_hierarchy_defaults_and_both_opt_outs(tmp_path: Path) -> None:
    settings = Config()
    mcp_registry.install(settings, local_dir=tmp_path / "plugins")
    snapshot = mcp_registry.catalog()
    registry = discover(user_dir=tmp_path / "skills", plugin_plays=snapshot.registered_plays)
    assert registry.play("hierarchy").provider == "beads"
    assert registry.load("hierarchy/master").provider == "beads"
    assert registry.play("hierarchy").rules.plugin_permissions["beads"]["member"] == (
        "read",
        "write",
    )
    refused = discover(
        user_dir=tmp_path / "skills",
        plugin_plays=snapshot.registered_plays,
        disabled=("hierarchy",),
    )
    assert refused.play("hierarchy") is None
    assert all(skill.play != "hierarchy" for skill in refused.skills)
    mcp_registry.install(
        replace(settings, mcp=replace(settings.mcp, enabled=[])), local_dir=tmp_path / "plugins"
    )
    assert not mcp_registry.catalog().registered_plays
