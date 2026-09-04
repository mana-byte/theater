"""Local cross-kind package diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from theater import cli, config
from theater import harness as harness_registry
from theater.cli.commands import introspection
from theater.mcp_plugins import registry as mcp_registry
from theater.plugins.diagnostics import describe

FIXTURES = Path(__file__).parent / "fixtures" / "mcp_plugins"
SECRET = "sentinel-plugin-secret"


def _manifest(root: Path, name: str, body: str) -> None:
    package = root / name
    package.mkdir(parents=True)
    (package / "manifest.py").write_text(body, encoding="utf-8")


def _mcp_manifest() -> str:
    return (
        "from theater.mcp_plugins import MANIFEST_API_VERSION, McpLaunchManifest\n"
        "from theater.mcp_plugins import McpLaunchPlan, McpServerManifest, PluginCapability\n"
        "MANIFEST = McpServerManifest(api_version=MANIFEST_API_VERSION, description='fixture', "
        "capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}), "
        "launch=McpLaunchManifest(planner=lambda _: McpLaunchPlan(command='fixture')))\n"
    )


def test_projection_is_deterministic_and_redacts_enabled_plugin_failures(
    monkeypatch, capsys, tmp_path
):
    local_mcp = tmp_path / "mcp_servers"
    local_harness = tmp_path / "harnesses"
    _manifest(local_mcp, "bomb", f"raise RuntimeError({SECRET!r})\n")
    _manifest(
        local_mcp,
        "configfail",
        "from theater.mcp_plugins import MANIFEST_API_VERSION, McpConfigField, McpConfigKind\n"
        "from theater.mcp_plugins import McpConfigSchema, McpLaunchManifest, McpLaunchPlan\n"
        "from theater.mcp_plugins import McpServerManifest, PluginCapability\n"
        "MANIFEST = McpServerManifest(api_version=MANIFEST_API_VERSION, description='fixture', "
        "capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}), "
        "launch=McpLaunchManifest(planner=lambda _: McpLaunchPlan(command='fixture')), "
        "config=McpConfigSchema({'token': McpConfigField(McpConfigKind.SECRET, required=True)}))\n",
    )
    _manifest(
        local_mcp,
        "wrongmcp",
        "from theater.harness.builtin.plugins.pi.manifest import MANIFEST\n",
    )
    _manifest(
        local_harness,
        "wrong",
        "from theater.mcp_plugins import McpLaunchManifest, McpLaunchPlan, McpServerManifest\n"
        "from theater.mcp_plugins import MANIFEST_API_VERSION, PluginCapability\n"
        "MANIFEST = McpServerManifest(api_version=MANIFEST_API_VERSION, description='wrong', "
        "capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}), "
        "launch=McpLaunchManifest(planner=lambda _: McpLaunchPlan(command='wrong')))\n",
    )
    settings = config.Config(
        mcp=config.McpSection(
            enabled=["native", "missing", "wrongmcp", "bomb", "configfail"],
            plugins={"configfail": {"token": SECRET}},
        ),
    )
    rows = describe(
        settings,
        harness_local_dir=local_harness,
        harness_shipped_dir=tmp_path / "shipped-harnesses",
        mcp_local_dir=local_mcp,
        mcp_shipped_dir=FIXTURES,
    )
    assert rows == sorted(rows, key=lambda row: (row["kind"], row["name"], row["source"] or ""))
    by_key = {(row["kind"], row["name"]): row for row in rows}
    assert by_key[("mcp_server", "native")]["state"] == "loaded"
    assert by_key[("mcp_server", "native")]["description"] == "Native fixture sidecar"
    assert by_key[("mcp_server", "native")]["capabilities"] == ["participants.read"]
    assert by_key[("mcp_server", "native")]["skills"] == ["native-fixture"]
    assert by_key[("mcp_server", "compat")]["state"] == "disabled"
    assert by_key[("mcp_server", "bomb")]["error"] == "manifest could not be loaded"
    assert (
        by_key[("mcp_server", "configfail")]["error"] == "enabled configuration could not be used"
    )
    assert by_key[("mcp_server", "missing")]["state"] == "missing"
    assert "HarnessManifest" in by_key[("mcp_server", "wrongmcp")]["error"]
    assert "McpServerManifest" in by_key[("harness", "wrong")]["error"]
    assert SECRET not in json.dumps(rows)

    monkeypatch.setattr(introspection, "describe_plugins", lambda _config: rows)
    monkeypatch.setattr(introspection.config, "load", lambda: settings)
    assert introspection.cmd_plugins(SimpleNamespace(json=False)) == 0
    assert SECRET not in capsys.readouterr().out
    assert introspection.cmd_plugins(SimpleNamespace(json=True)) == 0
    assert SECRET not in capsys.readouterr().out


def test_projection_matches_harness_and_mcp_precedence_and_duplicates(tmp_path):
    shipped_harness = tmp_path / "shipped-harnesses"
    local_harness = tmp_path / "harnesses"
    shipped_mcp = tmp_path / "shipped-mcp"
    local_mcp = tmp_path / "mcp_servers"
    harness_manifest = "from theater.harness.builtin.plugins.pi.manifest import MANIFEST\n"
    _manifest(shipped_harness, "sameharness", harness_manifest)
    _manifest(local_harness, "sameharness", harness_manifest)
    _manifest(local_harness, "dupharness", harness_manifest)
    (local_harness / "dupharness.py").write_text("x = 1\n", encoding="utf-8")
    _manifest(shipped_mcp, "sameserver", _mcp_manifest())
    _manifest(local_mcp, "sameserver", _mcp_manifest())
    _manifest(local_mcp, "dupeserver", _mcp_manifest())
    (local_mcp / "dupeserver.py").write_text("x = 1\n", encoding="utf-8")
    _manifest(local_harness, "collision", harness_manifest)
    _manifest(local_mcp, "collision", _mcp_manifest())
    rows = describe(
        config.Config(
            harness=config.HarnessSection(disabled=["collision"]),
            mcp=config.McpSection(enabled=["sameserver", "dupeserver"]),
        ),
        harness_local_dir=local_harness,
        harness_shipped_dir=shipped_harness,
        mcp_local_dir=local_mcp,
        mcp_shipped_dir=shipped_mcp,
    )
    by_key = {(row["kind"], row["name"], row["source"]): row for row in rows}
    assert by_key[("harness", "sameharness", "shipped")]["state"] == "overridden"
    assert by_key[("harness", "sameharness", "local")]["state"] == "loaded"
    assert by_key[("mcp_server", "sameserver", "shipped")]["state"] == "overridden"
    assert by_key[("mcp_server", "sameserver", "local")]["state"] == "loaded"
    harness_duplicate = [
        row for row in rows if row["kind"] == "harness" and row["name"] == "dupharness"
    ]
    assert [row["state"] for row in harness_duplicate] == ["loaded", "broken"]
    mcp_duplicate = [
        row for row in rows if row["kind"] == "mcp_server" and row["name"] == "dupeserver"
    ]
    assert [row["state"] for row in mcp_duplicate] == ["duplicate", "duplicate"]
    conflicts = [row for row in rows if row["name"] == "collision"]
    assert [row["state"] for row in conflicts] == ["conflict", "conflict"]
    assert all("prevents daemon startup" in row["error"] for row in conflicts)


def test_plugins_command_is_local_non_mutating_and_has_text_and_json(monkeypatch, capsys, tmp_path):
    local_mcp = tmp_path / "mcp_servers"
    _manifest(local_mcp, "acme", _mcp_manifest())
    settings = config.Config(mcp=config.McpSection(enabled=["acme"]))
    before_harnesses = dict(harness_registry.HARNESSES)
    before_harness_plugins = dict(harness_registry._PLUGINS)
    before_broken_harnesses = list(harness_registry._BROKEN)
    before_servers = dict(mcp_registry.MCP_SERVERS)
    before_mcp_plugins = dict(mcp_registry._PLUGINS)
    before_mcp_diagnostics = list(mcp_registry._DIAGNOSTICS)
    rows = describe(
        settings,
        harness_local_dir=tmp_path / "harnesses",
        harness_shipped_dir=tmp_path / "shipped-harnesses",
        mcp_local_dir=local_mcp,
        mcp_shipped_dir=tmp_path / "shipped-mcp",
    )
    assert before_harnesses == harness_registry.HARNESSES
    assert before_harness_plugins == harness_registry._PLUGINS
    assert before_broken_harnesses == harness_registry._BROKEN
    assert before_servers == mcp_registry.MCP_SERVERS
    assert before_mcp_plugins == mcp_registry._PLUGINS
    assert before_mcp_diagnostics == mcp_registry._DIAGNOSTICS

    monkeypatch.setattr(
        introspection,
        "describe_plugins",
        lambda loaded: describe(
            loaded,
            harness_local_dir=tmp_path / "harnesses",
            harness_shipped_dir=tmp_path / "shipped-harnesses",
            mcp_local_dir=local_mcp,
            mcp_shipped_dir=tmp_path / "shipped-mcp",
        ),
    )
    monkeypatch.setattr(introspection.config, "load", lambda: settings)
    monkeypatch.setattr(
        cli.harness_registry,
        "install",
        lambda _config: (_ for _ in ()).throw(AssertionError("plugins installed a registry")),
    )
    monkeypatch.setattr(
        introspection,
        "DaemonClient",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("plugins contacted daemon")),
    )
    assert cli.main(["plugins"]) == 0
    text = capsys.readouterr().out
    assert "mcp_server" in text
    assert "capabilities: participants.read" in text
    assert cli.main(["plugins", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == rows
