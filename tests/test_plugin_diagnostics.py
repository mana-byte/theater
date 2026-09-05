"""Behavioral coverage for the unified user-plugin catalog."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from theater import cli, config
from theater import harness as harness_registry
from theater.cli.commands import introspection
from theater.mcp_plugins import registry as mcp_registry
from theater.plugins.catalog import scan
from theater.plugins.diagnostics import describe

SECRET = "sentinel-plugin-secret"


def _manifest(root: Path, name: str, body: str) -> None:
    package = root / name
    package.mkdir(parents=True)
    (package / "manifest.py").write_text(body, encoding="utf-8")


def _mcp_manifest(*, secret: bool = False) -> str:
    config_lines = ""
    if secret:
        config_lines = (
            "from theater.mcp_plugins import McpConfigField, McpConfigKind, McpConfigSchema\n"
            "SCHEMA = McpConfigSchema({'token': "
            "McpConfigField(McpConfigKind.SECRET, required=True)})\n"
        )
    return (
        "from theater.mcp_plugins import MANIFEST_API_VERSION, McpLaunchManifest\n"
        "from theater.mcp_plugins import McpLaunchPlan, McpServerManifest, PluginCapability\n"
        f"{config_lines}"
        "MANIFEST = McpServerManifest(api_version=MANIFEST_API_VERSION, description='fixture', "
        "capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}), "
        "launch=McpLaunchManifest(planner=lambda _: McpLaunchPlan(command='fixture'))"
        f"{', config=SCHEMA' if secret else ''})\n"
    )


def _harness_manifest() -> str:
    return "from theater.harness.builtin.plugins.pi.manifest import MANIFEST\n"


def test_one_root_routes_both_plugin_types_and_redacts_failures(tmp_path):
    plugins = tmp_path / "plugins"
    _manifest(plugins, "worker", _harness_manifest())
    _manifest(plugins, "sidecar", _mcp_manifest())
    _manifest(plugins, "configfail", _mcp_manifest(secret=True))
    _manifest(plugins, "broken", f"raise RuntimeError({SECRET!r})\n")
    settings = config.Config(
        mcp=config.McpSection(
            enabled=["sidecar", "configfail", "missing"],
            plugins={"configfail": {"token": SECRET}},
        )
    )

    rows = describe(
        settings,
        local_dir=plugins,
        harness_shipped_dir=tmp_path / "shipped-harnesses",
        mcp_shipped_dir=tmp_path / "shipped-mcp",
    )
    by_key = {(row["kind"], row["name"]): row for row in rows}

    assert by_key[("harness", "worker")]["state"] == "loaded"
    assert by_key[("mcp_server", "sidecar")]["state"] == "loaded"
    assert by_key[("plugin", "broken")]["state"] == "broken"
    assert by_key[("mcp_server", "missing")]["state"] == "missing"
    assert by_key[("mcp_server", "configfail")]["state"] == "broken"
    assert SECRET not in json.dumps(rows)


def test_one_catalog_installs_both_plugin_types(tmp_path):
    plugins = tmp_path / "plugins"
    _manifest(plugins, "worker", _harness_manifest())
    _manifest(plugins, "sidecar", _mcp_manifest())

    installed = harness_registry.install(
        config.Config(mcp=config.McpSection(enabled=["sidecar"])),
        local_dir=plugins,
        shipped_dir=tmp_path / "shipped-harnesses",
        mcp_shipped_dir=tmp_path / "shipped-mcp",
    )

    assert installed == ["worker"]
    assert mcp_registry.get("sidecar").name == "sidecar"


def test_invalid_or_mixed_manifest_is_rejected_once(tmp_path):
    plugins = tmp_path / "plugins"
    _manifest(
        plugins,
        "mixed",
        "from theater.harness.builtin.plugins.pi.manifest import MANIFEST as HARNESS\n"
        + _mcp_manifest().replace("MANIFEST =", "MCP =")
        + "MANIFEST = (HARNESS, MCP)\n",
    )

    result = scan(plugins)

    assert not result.harnesses
    assert not result.mcp_servers
    assert len(result.rejected) == 1
    assert "exactly one" in result.rejected[0].error


def test_plugin_package_symlinks_are_supported(tmp_path):
    source = tmp_path / "source"
    plugins = tmp_path / "plugins"
    _manifest(source, "worker", _harness_manifest())
    plugins.mkdir()
    (plugins / "worker").symlink_to(source / "worker", target_is_directory=True)

    result = scan(plugins)

    assert [plugin.name for plugin in result.harnesses] == ["worker"]


def test_local_plugins_override_same_kind_and_cross_kind_names_conflict(tmp_path):
    plugins = tmp_path / "plugins"
    shipped_harnesses = tmp_path / "shipped-harnesses"
    shipped_mcps = tmp_path / "shipped-mcps"
    _manifest(shipped_harnesses, "worker", _harness_manifest())
    _manifest(plugins, "worker", _harness_manifest())
    _manifest(shipped_mcps, "sidecar", _mcp_manifest())
    _manifest(plugins, "sidecar", _mcp_manifest())
    _manifest(shipped_mcps, "collision", _mcp_manifest())
    _manifest(plugins, "collision", _harness_manifest())

    rows = describe(
        config.Config(mcp=config.McpSection(enabled=["sidecar"])),
        local_dir=plugins,
        harness_shipped_dir=shipped_harnesses,
        mcp_shipped_dir=shipped_mcps,
    )
    by_key = {(row["kind"], row["name"], row["source"]): row for row in rows}

    assert by_key[("harness", "worker", "shipped")]["state"] == "overridden"
    assert by_key[("harness", "worker", "local")]["state"] == "loaded"
    assert by_key[("mcp_server", "sidecar", "shipped")]["state"] == "overridden"
    assert by_key[("mcp_server", "sidecar", "local")]["state"] == "loaded"
    conflicts = [row for row in rows if row["name"] == "collision"]
    assert len(conflicts) == 2
    assert all(row["state"] == "conflict" for row in conflicts)


def test_plugins_command_is_local_and_does_not_mutate_registries(monkeypatch, capsys, tmp_path):
    plugins = tmp_path / "plugins"
    _manifest(plugins, "sidecar", _mcp_manifest())
    settings = config.Config(mcp=config.McpSection(enabled=["sidecar"]))
    before = (dict(harness_registry.HARNESSES), dict(mcp_registry.MCP_SERVERS))

    def project(_settings):
        return describe(
            _settings,
            local_dir=plugins,
            harness_shipped_dir=tmp_path / "shipped-harnesses",
            mcp_shipped_dir=tmp_path / "shipped-mcp",
        )

    monkeypatch.setattr(introspection, "describe_plugins", project)
    monkeypatch.setattr(introspection.config, "load", lambda: settings)
    monkeypatch.setattr(
        cli.harness_registry,
        "install",
        lambda _config: (_ for _ in ()).throw(AssertionError("registry mutated")),
    )

    assert introspection.cmd_plugins(SimpleNamespace(json=False)) == 0
    assert "sidecar" in capsys.readouterr().out
    assert introspection.cmd_plugins(SimpleNamespace(json=True)) == 0
    assert json.loads(capsys.readouterr().out) == project(settings)
    assert before == (dict(harness_registry.HARNESSES), dict(mcp_registry.MCP_SERVERS))
