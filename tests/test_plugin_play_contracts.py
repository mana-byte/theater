"""Plugin play discovery, pinned permissions, authenticated context and dependency admission."""

from dataclasses import replace

import pytest

from theater import paths
from theater.config import Config
from theater.daemon.plays import PlayPlan, PlayRefused
from theater.daemon.plugins.credentials import mint_credential
from theater.daemon.plugins.dispatch import dispatch
from theater.mcp_plugins import MANIFEST_API_VERSION, PluginCapability
from theater.mcp_plugins import registry as mcp_registry
from theater.skills import discover
from theater.skills.plays import parse_play_manifest

PLAY = """
description = "Plugin play."
master = "boss"
plugins = ["fixture"]
[positions]
boss = ["reader"]
reader = []
[plugin_permissions.fixture]
boss = ["read", "write"]
reader = ["read"]
"""


@pytest.fixture
def plugin_catalog(tmp_path, monkeypatch):
    package = tmp_path / "plugins" / "fixture"
    package.mkdir(parents=True)
    (package / "manifest.py").write_text(
        "from theater.mcp_plugins import *\n"
        "def plan(context): return McpLaunchPlan(command='fixture')\n"
        "MANIFEST = McpServerManifest(api_version=MANIFEST_API_VERSION, description='Fixture', "
        "capabilities=frozenset({PluginCapability.PLAYS_READ}), "
        "launch=McpLaunchManifest(plan), plays=('fixture-play',), "
        "play_permissions=frozenset({'read','write'}))\n"
    )
    play = package / "skills" / "fixture-play"
    play.mkdir(parents=True)
    (play / "PLAY.toml").write_text(PLAY)
    for name in ("boss", "reader"):
        member = play / name
        member.mkdir()
        (member / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Member.\n---\n\n# Member\n"
        )
    monkeypatch.setattr(mcp_registry, "MCP_SERVERS", {})
    monkeypatch.setattr(mcp_registry, "_PLUGINS", {})
    monkeypatch.setattr(mcp_registry, "_DIAGNOSTICS", [])
    settings = replace(Config(), mcp=replace(Config().mcp, enabled=["fixture"]))
    mcp_registry.install(settings, local_dir=package.parent, shipped_dir=tmp_path / "empty")
    return mcp_registry.catalog()


def test_plugin_play_loads_whole_and_rules_preserve_grants(plugin_catalog, tmp_path):
    registry = discover(user_dir=tmp_path / "skills", plugin_plays=plugin_catalog.registered_plays)
    play = registry.play("fixture-play")
    assert play.rules.plugins == ("fixture",)
    assert registry.load("fixture-play/reader").provider == "fixture"
    assert play.rules.plugin_permissions["fixture"]["reader"] == ("read",)
    restored = type(play.rules).from_json(play.rules.to_json())
    assert restored.sha256 == play.rules.sha256
    with pytest.raises(TypeError):
        restored.plugin_permissions["fixture"]["reader"] = ("write",)


def test_plugin_permissions_require_declared_dependencies_and_positions():
    for invalid in (
        PLAY.replace('plugins = ["fixture"]', "plugins = []"),
        PLAY.replace('reader = ["read"]', 'ghost = ["read"]'),
    ):
        with pytest.raises(ValueError):
            parse_play_manifest(invalid, play="fixture-play")


async def test_authenticated_context_and_dependency_admission(plugin_catalog, daemon):
    rules = parse_play_manifest(PLAY, play="fixture-play")[1]
    master = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    member = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=master.id)
    other = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    with daemon.store.write_unit() as unit:
        instance = daemon.store.plays.start(
            master.id, rules, created_at=0, connection=unit.connection
        )
        daemon.store.plays.add_member(
            member.id, instance, "reader", created_at=0, connection=unit.connection
        )
    material = mint_credential()
    daemon.store.set_mcp_plugin_credential(
        member.id,
        plugin_name="fixture",
        api_version=MANIFEST_API_VERSION,
        credential_id=material.credential_id,
        credential_verifier=material.verifier,
        grants={PluginCapability.PLAYS_READ},
        credential_path=str(paths.participant_dir(member.id) / "plugins/fixture/credential"),
    )
    record = daemon.store.get_mcp_plugin_credential(material.credential_id)
    context = await dispatch(daemon, record, "plays.context", {"participant_id": other.id})
    assert context["participant_id"] == member.id
    assert context["play"]["instance_id"] == instance
    assert context["play"]["permissions"] == ["read"]
    members = await dispatch(daemon, record, "plays.members", {})
    assert {item["participant_id"] for item in members} == {master.id, member.id}
    assert await dispatch(
        daemon, record, "plays.instances_status", {"instance_ids": [instance]}
    ) == [{"instance_id": instance, "active": True}]
    mcp_registry.MCP_SERVERS.clear()
    assert (await dispatch(daemon, record, "plays.context", {}))["enabled"] is False
    with pytest.raises(PlayRefused, match="requires enabled"):
        daemon.plays.check_spawn(master.id, PlayPlan(names=("fixture-play/reader",)))
    daemon.registry.mark_dead(master.id)
    assert (await dispatch(daemon, record, "plays.context", {}))["play"] is None
    assert await dispatch(
        daemon, record, "plays.instances_status", {"instance_ids": [instance]}
    ) == [{"instance_id": instance, "active": False}]
