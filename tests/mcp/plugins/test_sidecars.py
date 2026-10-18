"""Plugin sidecar planning: confined artifacts, credentials, collisions and omissions."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.plugin_sidecars import init_git_repo
from theater import paths
from theater.config import Config, McpSection
from theater.constants.plugins import MCP_PLUGIN_SPAWN_OMISSION_MAX
from theater.daemon.schema import participant_mcp_plugins
from theater.daemon.spawning import planning
from theater.daemon.spawning.models import SpawnRequest
from theater.daemon.spawning.service import Spawner
from theater.harness.base import LaunchPlan
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.launch import ChannelCredential
from theater.mcp_plugins import (
    MCP_PLUGIN_API_VERSION,
    CompiledMcpPlugin,
    McpLaunchManifest,
    McpLaunchPlan,
    PluginCapability,
)
from theater.mcp_plugins import registry as mcp_registry
from theater.mcp_plugins.registry import McpPluginDiagnostic


def _plugin(
    *,
    name: str = "acme",
    capabilities: frozenset[PluginCapability] = frozenset({PluginCapability.PARTICIPANTS_READ}),
    planner=None,
) -> CompiledMcpPlugin:
    if planner is None:

        def planner(_context) -> McpLaunchPlan:
            return McpLaunchPlan(command="acme-server")

    return CompiledMcpPlugin(
        name=name,
        description="test MCP sidecar",
        capabilities=capabilities,
        config={},
        launch=McpLaunchManifest(planner=planner),
    )


@pytest.fixture
def isolated_mcp_registry():
    prior = dict(mcp_registry.MCP_SERVERS)
    prior_plugins = dict(mcp_registry._PLUGINS)
    prior_diagnostics = list(mcp_registry._DIAGNOSTICS)
    mcp_registry.MCP_SERVERS.clear()
    mcp_registry._PLUGINS.clear()
    mcp_registry._DIAGNOSTICS.clear()
    try:
        yield mcp_registry.MCP_SERVERS
    finally:
        mcp_registry.MCP_SERVERS.clear()
        mcp_registry.MCP_SERVERS.update(prior)
        mcp_registry._PLUGINS.clear()
        mcp_registry._PLUGINS.update(prior_plugins)
        mcp_registry._DIAGNOSTICS.clear()
        mcp_registry._DIAGNOSTICS.extend(prior_diagnostics)


@pytest.fixture
def rendering_sidecars(monkeypatch):
    monkeypatch.setattr(planning, "supports_mcp_rendering", lambda _harness: True)


def _request() -> SpawnRequest:
    return SpawnRequest(harness="fake", prompt="", cwd="/tmp", approval="manual")


async def test_sidecar_planning_materializes_confined_artifacts_and_a_0600_credential(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    state_dirs: list[Path] = []

    def sidecar_plan(context) -> McpLaunchPlan:
        state_dirs.append(context.state_dir)
        return McpLaunchPlan(
            command="acme-server",
            argv=("--stdio",),
            env={"ACME_MODE": "test"},
            files={Path("public/config.json"): "{}"},
            private_files={Path("private/config.secret"): "secret"},
        )

    isolated_mcp_registry["acme"] = _plugin(
        capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}),
        planner=sidecar_plan,
    )
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    observed: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        observed.append(mcp_servers)
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = await planning.build_plan(_request(), participant, None, registry=registry)

    assert len(observed) == 1
    assert [item.name for item in observed[0]] == ["theater", "theater_wait", "acme"]
    spec = observed[0][-1]
    assert spec.name == "acme"
    rendered_spec = json.dumps({"command": spec.command, "args": spec.args, "env": dict(spec.env)})
    assert "acme-server" not in rendered_spec
    assert "--stdio" not in rendered_spec
    assert "ACME_MODE" not in rendered_spec
    root = paths.participant_plugin_dir(participant.id, "acme")
    assert state_dirs == [paths.plugin_state_dir("acme")]
    assert state_dirs[0].is_dir()
    assert stat.S_IMODE(state_dirs[0].stat().st_mode) == 0o700
    assert set(plan.files) == {root / "public/config.json"}
    assert root / "private/config.secret" in plan.private_files
    (record,) = registry.store.mcp_plugin_credentials(participant.id)
    credential_path = Path(record.credential_path)
    assert credential_path.parent.resolve() == root.resolve()
    assert credential_path.resolve() in {path.resolve() for path in plan.private_files}

    planning.record_plan_artifacts(participant, plan, registry)
    planning.write_plan_files(plan)
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in plan.private_files)
    assert stat.S_IMODE(credential_path.parent.stat().st_mode) == 0o700

    assert record.plugin_name == "acme"
    assert record.api_version == MCP_PLUGIN_API_VERSION
    assert record.grants == frozenset({PluginCapability.PARTICIPANTS_READ})
    row = registry.store.conn.execute(participant_mcp_plugins.select()).first()
    assert row is not None
    assert credential_path.read_text().strip() not in json.dumps(dict(row._mapping))


async def test_detached_runtime_and_fallback_share_the_same_sidecars(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    rendered: list[tuple[str, ...]] = []

    def render(_harness, *, plan, mcp_servers, **_kwargs):
        rendered.append(tuple(server.name for server in mcp_servers))
        return plan

    monkeypatch.setattr(planning, "overlay_mcp", render)
    backend, fallback = planning.install_runtime_mcp_plans(
        LaunchPlan(argv=["backend"]),
        LaunchPlan(argv=["fallback"]),
        participant,
        store=registry.store,
    )

    expected = ("theater", "theater_wait", "acme")
    assert rendered == [expected, expected]
    assert backend.private_files == fallback.private_files
    assert len(registry.store.mcp_plugin_credentials(participant.id)) == 1


async def test_private_sidecar_launch_executes_with_its_environment(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    secret = "sidecar-environment-secret"
    isolated_mcp_registry["acme"] = _plugin(
        planner=lambda _context: McpLaunchPlan(
            command=sys.executable,
            argv=("-c", "import os; print(os.environ['PLUGIN_TOKEN'])"),
            env={"PLUGIN_TOKEN": secret},
        )
    )
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    rendered = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        rendered.append(mcp_servers[-1])
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = await planning.build_plan(_request(), participant, None, registry=registry)
    planning.write_plan_files(plan)
    spec = rendered[0]

    result = subprocess.run(  # noqa: ASYNC221
        [spec.command, *spec.args],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, **spec.env},
    )

    assert result.returncode == 0
    assert result.stdout.strip() == secret
    assert secret not in repr(spec)


@pytest.mark.parametrize(
    ("harness", "approval"),
    (
        ("claude", "manual"),
        ("codex", "manual"),
        ("opencode", "yolo"),
        ("pi", "yolo"),
        ("vibe", "manual"),
    ),
)
async def test_sidecar_environment_is_private_across_shipped_harnesses(
    harness, approval, registry, isolated_mcp_registry, available_harness_binaries
):
    secret = "sidecar-environment-secret"
    isolated_mcp_registry["acme"] = _plugin(
        planner=lambda _context: McpLaunchPlan(
            command="acme-server",
            argv=("--token", secret),
            env={"ACME_TOKEN": secret},
        )
    )
    participant = registry.create_spawned(harness=harness, cwd="/tmp")
    request = SpawnRequest(harness=harness, prompt="", cwd="/tmp", approval=approval)

    plan = await planning.build_plan(request, participant, None, registry=registry)

    assert secret not in "\0".join(plan.argv)
    assert secret not in json.dumps(plan.env)
    assert all(secret not in contents for contents in plan.files.values())
    assert any(secret in contents for contents in plan.private_files.values())


def test_launch_plan_repr_redacts_sidecar_and_native_credentials():
    channel = ChannelCredential(
        kind=ChannelKind.HOOK,
        channel_id="native",
        token="channel-token-secret",
        token_path=Path("/tmp/channel-token"),
    )
    plan = LaunchPlan(
        argv=["fake"],
        private_files={
            Path("/tmp/plugin-credential"): "sidecar-credential-secret",
            Path("/tmp/private-artifact"): "private-artifact-secret",
        },
        receipt_token="receipt-token-secret",
        channel_credentials=(channel,),
    )

    rendered = repr(plan)
    for secret in (
        "sidecar-credential-secret",
        "private-artifact-secret",
        "receipt-token-secret",
        "channel-token-secret",
    ):
        assert secret not in rendered
    assert "channel-token-secret" not in repr(channel)


async def test_sidecar_planner_receives_the_persisted_worktree_cwd(
    monkeypatch,
    registry,
    isolated_mcp_registry,
    rendering_sidecars,
    tmp_path,
    available_harness_binaries,
):
    source = tmp_path / "source"
    init_git_repo(source)
    planned_cwds: list[Path] = []

    def sidecar_plan(context) -> McpLaunchPlan:
        planned_cwds.append(context.cwd)
        return McpLaunchPlan(command="acme-server")

    isolated_mcp_registry["acme"] = _plugin(planner=sidecar_plan)
    monkeypatch.setattr(
        planning,
        "plan_launch",
        lambda _harness, **_kwargs: LaunchPlan(argv=["fake"]),
    )
    spawner = Spawner(registry)
    reservation = await spawner.reserve(
        SpawnRequest(
            harness="vibe",
            prompt="",
            cwd=str(source),
            approval="manual",
            worktree=True,
        )
    )
    try:
        assert Path(reservation.child_cwd) != source
        assert planned_cwds == [Path(reservation.child_cwd)]
        assert registry.get(reservation.participant.id).cwd == reservation.child_cwd
    finally:
        await spawner.cleanup_reservation(reservation.participant)


async def test_symlinked_or_broken_sidecars_are_omitted_without_failing_the_harness_plan(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars, tmp_path
):
    good = _plugin(name="good")

    def bad_plan(context) -> McpLaunchPlan:
        plugin_root = paths.participant_plugin_dir(context.participant_id, "bad")
        plugin_root.mkdir(parents=True, exist_ok=True)
        (plugin_root / "nested").symlink_to(tmp_path / "outside")
        return McpLaunchPlan(command="bad-server", files={Path("nested/config"): "x"})

    isolated_mcp_registry["bad"] = _plugin(name="bad", planner=bad_plan)
    isolated_mcp_registry["good"] = good
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    seen: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        seen.append(mcp_servers)
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = await planning.build_plan(_request(), participant, None, registry=registry)

    assert plan.argv == ["fake"]
    assert [item.name for item in seen[0]] == ["theater", "theater_wait", "good"]
    attached = registry.store.mcp_plugin_credentials(participant.id)
    assert [item.plugin_name for item in attached] == ["good"]
    events = registry.store.bus_tail(limit=20)
    assert any(
        event["kind"] == "mcp_plugin.omitted" and event["payload"]["plugin"] == "bad"
        for event in events
    )


async def test_sidecar_artifact_collisions_are_omitted_without_overwriting_harness_files(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    def sidecar_plan(_context) -> McpLaunchPlan:
        return McpLaunchPlan(command="acme-server", files={Path("config"): "sidecar"})

    isolated_mcp_registry["acme"] = _plugin(planner=sidecar_plan)
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    root = paths.participant_plugin_dir(participant.id, "acme")

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        return LaunchPlan(argv=["fake"], files={root / "config" / "harness.json": "{}"})

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = await planning.build_plan(_request(), participant, None, registry=registry)

    assert plan.files == {root / "config" / "harness.json": "{}"}
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert root / ".theater-plugin-credential" not in plan.private_files


async def test_sidecar_collision_filter_converges_after_each_renderer_change(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    for name in ("alpha", "beta", "gamma"):
        isolated_mcp_registry[name] = _plugin(
            name=name,
            planner=lambda _context: McpLaunchPlan(
                command="sidecar",
                files={Path("config"): "sidecar"},
            ),
        )
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    roots = {
        name: paths.participant_plugin_dir(participant.id, name)
        for name in ("alpha", "beta", "gamma")
    }

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        names = [server.name for server in mcp_servers]
        if "alpha" in names:
            path = roots["alpha"] / "config" / "harness.json"
        elif "beta" in names:
            path = roots["beta"] / "config" / "harness.json"
        elif "gamma" in names:
            path = roots["gamma"] / "config" / "harness.json"
        else:
            path = Path("/tmp/core-config.json")
        return LaunchPlan(argv=["fake"], files={path: "{}"})

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = await planning.build_plan(_request(), participant, None, registry=registry)

    assert plan.files == {Path("/tmp/core-config.json"): "{}"}
    assert registry.store.mcp_plugin_credentials(participant.id) == ()


async def test_sidecar_persistence_failure_rolls_back_its_record_and_empty_root(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    root = paths.participant_plugin_dir(participant.id, "acme")
    persist = registry.store.set_mcp_plugin_credential

    def persist_then_fail(*args, **kwargs) -> None:
        persist(*args, **kwargs)
        raise RuntimeError("simulated persistence failure")

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        assert [item.name for item in mcp_servers] == ["theater", "theater_wait"]
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(registry.store, "set_mcp_plugin_credential", persist_then_fail)
    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = await planning.build_plan(_request(), participant, None, registry=registry)
    assert plan.argv == ["fake"]

    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert not root.exists()


async def test_harness_rejection_of_sidecars_omits_them_without_failing_the_spawn_plan(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    attempts: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        attempts.append(mcp_servers)
        if any(server.name == "acme" for server in mcp_servers):
            raise ValueError("renderer cannot use this sidecar")
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)
    plan = await planning.build_plan(_request(), participant, None, registry=registry)
    assert plan.argv == ["fake"]

    assert [item.name for item in attempts[0]] == ["theater", "theater_wait", "acme"]
    assert [item.name for item in attempts[1]] == ["theater", "theater_wait"]
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert any(
        event["kind"] == "mcp_plugin.omitted" and event["payload"]["plugin"] == "acme"
        for event in registry.store.bus_tail(limit=20)
    )


async def test_build_plan_renders_core_servers_with_empty_and_configured_plugin_sets(
    registry, isolated_mcp_registry
):
    request = SpawnRequest(harness="codex", prompt="", cwd="/tmp", approval="manual")
    empty = registry.create_spawned(harness="codex", cwd="/tmp")

    empty_plan = await planning.build_plan(request, empty, None, registry=registry)
    assert _rendered_codex_server_names(empty_plan) == {"theater", "theater_wait"}

    isolated_mcp_registry["acme"] = _plugin()
    configured = registry.create_spawned(harness="codex", cwd="/tmp")

    configured_plan = await planning.build_plan(request, configured, None, registry=registry)
    assert _rendered_codex_server_names(configured_plan) == {"acme", "theater", "theater_wait"}


async def test_unrenderable_harness_omits_sidecars_without_persisting_credentials(
    monkeypatch, registry, isolated_mcp_registry
):
    isolated_mcp_registry["acme"] = _plugin()
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    observed: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        observed.append(mcp_servers)
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "supports_mcp_rendering", lambda _harness: False)
    monkeypatch.setattr(planning, "plan_launch", harness_plan)

    plan = await planning.build_plan(_request(), participant, None, registry=registry)
    assert plan.argv == ["fake"]
    assert [item.name for item in observed[0]] == ["theater", "theater_wait"]
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert any(
        event["kind"] == "mcp_plugin.omitted"
        and event["payload"]
        == {
            "plugin": "acme",
            "stage": "rendering",
            "error": (
                "ValueError: the selected harness does not render generic MCP server specifications"
            ),
        }
        for event in registry.store.bus_tail(limit=20)
    )


@pytest.mark.parametrize("name", ("theater", "theater_wait"))
async def test_reserved_core_server_names_are_omitted_without_shadowing_core_specs(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars, name
):
    isolated_mcp_registry[name] = _plugin(name=name)
    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    observed: list[tuple] = []

    def harness_plan(_harness, *, mcp_servers=(), **_kwargs) -> LaunchPlan:
        observed.append(mcp_servers)
        return LaunchPlan(argv=["fake"])

    monkeypatch.setattr(planning, "plan_launch", harness_plan)

    plan = await planning.build_plan(_request(), participant, None, registry=registry)
    assert plan.argv == ["fake"]
    assert [item.name for item in observed[0]] == ["theater", "theater_wait"]
    assert registry.store.mcp_plugin_credentials(participant.id) == ()
    assert any(
        event["kind"] == "mcp_plugin.omitted"
        and event["payload"]["plugin"] == name
        and "reserved by Theater" in event["payload"]["error"]
        for event in registry.store.bus_tail(limit=20)
    )


async def test_registry_diagnostics_emit_bounded_safe_spawn_omissions(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars
):
    for index in range(MCP_PLUGIN_SPAWN_OMISSION_MAX + 1):
        mcp_registry._DIAGNOSTICS.append(
            McpPluginDiagnostic(name=f"broken{index}", error=f"secret-{index}", requested=True)
        )
    participant = registry.create_spawned(harness="fake", cwd="/tmp")

    monkeypatch.setattr(
        planning,
        "plan_launch",
        lambda _harness, **_kwargs: LaunchPlan(argv=["fake"]),
    )

    plan = await planning.build_plan(_request(), participant, None, registry=registry)
    assert plan.argv == ["fake"]
    omissions = [
        event
        for event in registry.store.bus_tail(limit=MCP_PLUGIN_SPAWN_OMISSION_MAX + 5)
        if event["kind"] == "mcp_plugin.omitted" and event["payload"]["stage"] == "registry"
    ]
    assert len(omissions) == MCP_PLUGIN_SPAWN_OMISSION_MAX
    assert all("secret-" not in event["payload"]["error"] for event in omissions)


async def test_disabled_malformed_package_does_not_emit_a_spawn_omission(
    monkeypatch, registry, isolated_mcp_registry, rendering_sidecars, tmp_path
):
    package = tmp_path / "plugins" / "broken"
    package.mkdir(parents=True)
    assert (
        mcp_registry.install(
            Config(mcp=McpSection(enabled=[])),
            local_dir=package.parent,
            shipped_dir=tmp_path / "shipped",
        )
        == []
    )
    assert mcp_registry.diagnostics() == ()

    participant = registry.create_spawned(harness="fake", cwd="/tmp")
    monkeypatch.setattr(
        planning,
        "plan_launch",
        lambda _harness, **_kwargs: LaunchPlan(argv=["fake"]),
    )

    plan = await planning.build_plan(_request(), participant, None, registry=registry)
    assert plan.argv == ["fake"]
    assert not any(
        event["kind"] == "mcp_plugin.omitted" and event["payload"]["stage"] == "registry"
        for event in registry.store.bus_tail(limit=20)
    )


def _rendered_codex_server_names(plan: LaunchPlan) -> set[str]:
    return {
        argument.removeprefix("mcp_servers.").split(".", 1)[0]
        for argument in plan.argv
        if argument.startswith("mcp_servers.") and ".command=" in argument
    }
