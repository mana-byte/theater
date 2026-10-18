"""Plugin play discovery, pinned permissions, authenticated context and dependency admission."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from theater import paths
from theater.config import Config
from theater.daemon.plays import PlayPlan, PlayRefused
from theater.daemon.plugins.credentials import mint_credential
from theater.daemon.plugins.dispatch import dispatch
from theater.daemon.spawning import planning
from theater.daemon.spawning.models import SpawnRequest
from theater.daemon.spawning.service import Spawner
from theater.harness.base import LaunchPlan
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
def plugin_catalog(daemon, tmp_path, monkeypatch):
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
    daemon.store.delete_mcp_plugin_credential(member.id, "fixture")
    with pytest.raises(PlayRefused, match="attached to this participant"):
        daemon.plays.check_send(master.id, member.id, PlayPlan(names=("fixture-play/reader",)))
    from theater.daemon.persistence.repositories.plays import PlayAssignment

    pending = PlayAssignment("queued", member.id, instance, "reader", master.id)
    assert "attached to this participant" in daemon.plays._invalid(pending)
    mcp_registry.MCP_SERVERS.clear()
    assert (await dispatch(daemon, record, "plays.context", {}))["enabled"] is False
    with pytest.raises(PlayRefused, match="requires enabled"):
        daemon.plays.check_spawn(master.id, PlayPlan(names=("fixture-play/reader",)))
    daemon.registry.mark_dead(master.id)
    assert (await dispatch(daemon, record, "plays.context", {}))["play"] is None
    assert await dispatch(
        daemon, record, "plays.instances_status", {"instance_ids": [instance]}
    ) == [{"instance_id": instance, "active": False}]


async def test_dependent_play_requires_existing_and_rendered_attachments(
    plugin_catalog, daemon, monkeypatch
):
    registry = discover(plugin_plays=plugin_catalog.registered_plays)
    play = registry.play("fixture-play")
    master = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
    with pytest.raises(PlayRefused, match="attached to this participant"):
        daemon.plays.start_by_load(master.id, play)
    with daemon.store.write_unit() as unit:
        daemon.store.plays.start(master.id, play.rules, created_at=0, connection=unit.connection)
    monkeypatch.setattr(planning, "supports_mcp_rendering", lambda _name: True)
    monkeypatch.setattr(planning, "plan_sidecars", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        planning, "plan_launch", lambda *_args, **_kwargs: LaunchPlan(argv=["vibe"])
    )
    monkeypatch.setattr(planning, "overlay_mcp", lambda _name, *, plan, **_kwargs: plan)
    with pytest.raises(PlayRefused, match="could not attach required"):
        await planning.build_plan(
            SpawnRequest(harness="vibe", prompt="work", cwd="/tmp", approval="manual"),
            master,
            None,
            registry=daemon.registry,
        )
    with pytest.raises(PlayRefused, match="could not attach required"):
        planning.install_runtime_mcp_plans(
            LaunchPlan(argv=["vibe"]), None, master, store=daemon.store
        )
    spawner = Spawner.__new__(Spawner)
    spawner.registry = daemon.registry
    monkeypatch.setattr(spawner, "_preserve_failed_launch", lambda _participant: None)
    with pytest.raises(PlayRefused, match="could not attach required"):
        await spawner._launch_legacy_fallback(
            SimpleNamespace(
                participant=master,
                req=SimpleNamespace(harness="vibe"),
                legacy_plan=LaunchPlan(argv=["vibe"]),
            )
        )


def test_plugin_play_collision_omits_every_member(plugin_catalog, tmp_path):
    package = tmp_path / "skills" / "fixture-play"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(
        "---\nname: fixture-play\ndescription: Conflicting skill.\n---\n\n# Existing\n"
    )
    registry = discover(user_dir=package.parent, plugin_plays=plugin_catalog.registered_plays)
    assert registry.play("fixture-play") is None
    assert all(not skill.name.startswith("fixture-play/") for skill in registry.skills)
    assert registry.rejections[0].provider == "fixture"


def test_provider_rejects_its_unsupported_play_grants(plugin_catalog):
    from theater.mcp_plugins.loading import load_plugin
    from theater.plugins.catalog import scan

    loaded = mcp_registry._PLUGINS["fixture"]
    manifest = loaded.path / "skills/fixture-play/PLAY.toml"
    manifest.write_text(PLAY.replace('boss = ["read", "write"]', 'boss = ["unknown"]'))
    assert "unsupported" in load_plugin(loaded).error
    assert "unsupported" in scan(loaded.path.parent).rejected[0].error
