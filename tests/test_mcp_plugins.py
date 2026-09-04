"""Focused contracts for the Wave 1 MCP-server plugin foundation."""

from __future__ import annotations

from pathlib import Path

import pytest

from theater import config, harness, paths
from theater.mcp_plugins import (
    MANIFEST_API_VERSION,
    CompiledMcpPlugin,
    McpConfigField,
    McpConfigKind,
    McpConfigSchema,
    McpLaunchManifest,
    McpLaunchPlan,
    McpPluginError,
    McpServerManifest,
    McpServerSpec,
    PluginCapability,
    registry,
)
from theater.mcp_plugins.loading import LOCAL, discover, load_plugin

MANIFEST = """
from pathlib import Path
from theater.mcp_plugins import (
    MANIFEST_API_VERSION,
    McpConfigField,
    McpConfigKind,
    McpConfigSchema,
    McpLaunchManifest,
    McpLaunchPlan,
    McpServerManifest,
    PluginCapability,
)

def plan(context):
    return McpLaunchPlan(
        command={command!r},
        argv=("--id", context.participant_id),
        env={{"ACME_TOKEN": context.config["token"].value}},
        files={{Path("public/config.txt"): context.config["endpoint"]}},
        private_files={{Path("private/token.txt"): context.config["token"].value}},
    )

MANIFEST = McpServerManifest(
    api_version=MANIFEST_API_VERSION,
    description="Acme test sidecar",
    capabilities=frozenset({{PluginCapability.PARTICIPANTS_READ}}),
    launch=McpLaunchManifest(planner=plan),
    config=McpConfigSchema({{
        "endpoint": McpConfigField(McpConfigKind.STRING, default="https://default.invalid"),
        "token": McpConfigField(McpConfigKind.SECRET, required=True),
    }}),
)
"""


def write_plugin(root: Path, name: str = "acme", *, command: str = "acme-server") -> Path:
    package = root / name
    package.mkdir(parents=True)
    manifest = package / "manifest.py"
    manifest.write_text(MANIFEST.format(command=command), encoding="utf-8")
    return manifest


@pytest.fixture(autouse=True)
def clean_mcp_registry():
    registry.MCP_SERVERS.clear()
    registry._PLUGINS.clear()
    registry._DIAGNOSTICS.clear()
    yield
    registry.MCP_SERVERS.clear()
    registry._PLUGINS.clear()
    registry._DIAGNOSTICS.clear()


def test_enabled_plugin_resolves_immutable_config_once_and_redacts(tmp_path, monkeypatch):
    local = tmp_path / "mcp_servers"
    shipped = tmp_path / "shipped"
    write_plugin(local)
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "[mcp]\n"
        'enabled = ["acme"]\n\n'
        "[mcp.plugins.acme]\n"
        'endpoint = "https://acme.invalid"\n'
        'token = { env = "ACME_TEST_TOKEN" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("ACME_TEST_TOKEN", "first-secret")

    loaded = config.load(config_path)
    assert registry.install(loaded, local_dir=local, shipped_dir=shipped) == ["acme"]
    monkeypatch.setenv("ACME_TEST_TOKEN", "later-secret")

    plugin = registry.get("acme")
    assert isinstance(plugin, CompiledMcpPlugin)
    assert plugin.config["endpoint"] == "https://acme.invalid"
    assert plugin.config["token"].value == "first-secret"
    assert "first-secret" not in repr(plugin.config)
    with pytest.raises(TypeError):
        plugin.config["endpoint"] = "other"  # type: ignore[index]
    plan = plugin.plan_launch(participant_id="p-1", cwd=tmp_path)
    assert plan.command == "acme-server"
    assert plan.argv == ("--id", "p-1")
    assert plan.files == {Path("public/config.txt"): "https://acme.invalid"}
    assert plan.private_files == {Path("private/token.txt"): "first-secret"}
    assert "first-secret" not in repr(plan)


def test_mcp_server_root_is_created_under_theater_home(theater_home):
    paths.ensure_home()
    assert paths.mcp_servers_dir() == theater_home / "mcp_servers"
    assert paths.mcp_servers_dir().is_dir()


def test_harness_registry_installs_enabled_mcp_servers_at_startup(tmp_path, monkeypatch):
    local = tmp_path / "mcp_servers"
    write_plugin(local)
    monkeypatch.setenv("ACME_TEST_TOKEN", "secret")
    settings = config.Config(
        mcp=config.McpSection(
            enabled=["acme"],
            plugins={"acme": {"token": {"env": "ACME_TEST_TOKEN"}}},
        )
    )

    assert (
        harness.install(
            settings,
            local_dir=tmp_path / "local-harnesses",
            shipped_dir=tmp_path / "shipped-harnesses",
            mcp_local_dir=local,
            mcp_shipped_dir=tmp_path / "shipped-mcp",
        )
        == []
    )
    assert registry.get("acme").name == "acme"


def test_disabled_package_is_discovered_without_importing_its_manifest(tmp_path):
    local = tmp_path / "mcp_servers"
    package = local / "bomb"
    package.mkdir(parents=True)
    (package / "manifest.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")

    loaded = config.Config()
    assert registry.install(loaded, local_dir=local, shipped_dir=tmp_path / "shipped") == []
    assert registry.diagnostics() == ()


def test_disabled_package_still_reserves_the_global_canonical_name(tmp_path):
    local = tmp_path / "mcp_servers"
    write_plugin(local, "vibe")

    with pytest.raises(config.ConfigError, match="conflicts with harness"):
        harness.install(
            config.Config(),
            local_dir=tmp_path / "no-local-harnesses",
            mcp_local_dir=local,
            mcp_shipped_dir=tmp_path / "shipped-mcp",
        )


def test_local_package_overrides_shipped_package_of_the_same_kind(tmp_path, monkeypatch):
    local = tmp_path / "mcp_servers"
    shipped = tmp_path / "shipped"
    write_plugin(shipped, command="shipped-server")
    write_plugin(local, command="local-server")
    monkeypatch.setenv("ACME_TEST_TOKEN", "secret")
    settings = config.Config(
        mcp=config.McpSection(
            enabled=["acme"],
            plugins={"acme": {"token": {"env": "ACME_TEST_TOKEN"}}},
        )
    )

    assert registry.install(settings, local_dir=local, shipped_dir=shipped) == ["acme"]
    plan = registry.get("acme").plan_launch(participant_id="p", cwd=tmp_path)
    assert plan.command == "local-server"


def test_wrong_manifest_type_is_a_path_qualified_broken_result(tmp_path):
    root = tmp_path / "mcp_servers"
    package = root / "wrong"
    package.mkdir(parents=True)
    manifest_path = package / "manifest.py"
    manifest_path.write_text("MANIFEST = object()\n", encoding="utf-8")

    result = load_plugin(discover(root, source=LOCAL)[0])
    assert result.error is not None
    assert str(manifest_path) in result.error
    assert "McpServerManifest" in result.error


def test_unknown_enabled_and_broken_local_packages_are_diagnostics_not_config_errors(tmp_path):
    local = tmp_path / "mcp_servers"
    (local / "broken").mkdir(parents=True)
    settings = config.Config(mcp=config.McpSection(enabled=["broken", "missing"]))

    assert registry.install(settings, local_dir=local, shipped_dir=tmp_path / "shipped") == []
    messages = {item.name: item.error for item in registry.diagnostics()}
    assert "manifest.py" in messages["broken"]
    assert "not found" in messages["missing"]


def test_enabled_broken_shipped_package_is_fatal(tmp_path):
    shipped = tmp_path / "shipped"
    package = shipped / "broken"
    package.mkdir(parents=True)
    (package / "manifest.py").write_text(
        "raise RuntimeError('broken shipped package')",
        encoding="utf-8",
    )

    with pytest.raises(McpPluginError, match="broken shipped package"):
        registry.install(
            config.Config(mcp=config.McpSection(enabled=["broken"])),
            local_dir=tmp_path / "mcp_servers",
            shipped_dir=shipped,
        )


@pytest.mark.parametrize(
    "plan",
    [
        lambda: McpLaunchPlan(command="server", files={Path("/absolute"): "x"}),
        lambda: McpLaunchPlan(command="server", private_files={Path("../escape"): "x"}),
        lambda: McpLaunchPlan(command="server", argv="--bad"),
        lambda: McpLaunchPlan(command="server", env={"NOT-VALID!": "x"}),
    ],
)
def test_launch_plan_rejects_invalid_artifacts_and_process_values(plan):
    with pytest.raises((TypeError, ValueError)):
        plan()


def test_mcp_server_spec_is_an_immutable_renderer_ready_stdio_endpoint():
    server = McpServerSpec(
        name="acme",
        command="acme-server",
        args=["--stdio", "--quiet"],
        env={"ACME_MODE": "test"},
    )

    assert server.name == "acme"
    assert server.command == "acme-server"
    assert server.args == ("--stdio", "--quiet")
    assert server.env == {"ACME_MODE": "test"}
    assert tuple(McpServerSpec.__dataclass_fields__) == ("name", "command", "args", "env")
    assert not hasattr(server, "capabilities")
    assert not hasattr(server, "config")
    assert not hasattr(server, "launch")
    with pytest.raises(TypeError):
        server.env["ACME_MODE"] = "other"  # type: ignore[index]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": "Bad", "command": "acme-server"},
        {"name": "acme", "command": "   "},
        {"name": "acme", "command": "acme-server", "args": "--stdio"},
        {"name": "acme", "command": "acme-server", "env": {"BAD-NAME": "x"}},
    ],
)
def test_mcp_server_spec_rejects_invalid_endpoint_values(kwargs):
    with pytest.raises((TypeError, ValueError)):
        McpServerSpec(**kwargs)


def test_mcp_config_shell_rejects_bad_structure_but_not_disabled_plugin_values(tmp_path):
    malformed = tmp_path / "malformed.toml"
    malformed.write_text("[mcp]\nenabled = 'acme'\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match=r"mcp\.enabled"):
        config.load(malformed)

    disabled = tmp_path / "disabled.toml"
    disabled.write_text(
        "[mcp]\nenabled = []\n\n[mcp.plugins.acme]\nunknown = { nested = true }\n",
        encoding="utf-8",
    )
    loaded = config.load(disabled)
    assert loaded.mcp.plugins["acme"]["unknown"] == {"nested": True}


def test_manifest_contract_requires_whole_nonempty_capability_set():
    manifest = McpServerManifest(
        api_version=MANIFEST_API_VERSION,
        description="Acme",
        capabilities=frozenset({PluginCapability.JOBS_READ}),
        launch=McpLaunchManifest(planner=lambda _context: McpLaunchPlan(command="acme")),
        config=McpConfigSchema({"count": McpConfigField(McpConfigKind.INTEGER, minimum=1)}),
    )
    assert manifest.capabilities == frozenset({PluginCapability.JOBS_READ})
    assert PluginCapability.SESSIONS_KILL.value == "sessions.kill"
