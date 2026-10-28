"""Plugin diagnostics and describe projections bound untrusted manifest output."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.plugin_catalog_rig import local_dir as shared_local_dir
from tests.support.plugin_packages import install
from theater import cli, config
from theater import harness as harness_registry
from theater.cli.commands import introspection
from theater.constants.harness import (
    HARNESS_DIAGNOSTICS_MAX_BINDINGS,
    HARNESS_DIAGNOSTICS_MAX_CHANNELS,
    HARNESS_DIAGNOSTICS_MAX_PARTICIPANTS,
    HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS,
)
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelHealth,
    ChannelKind,
    HookBinding,
)
from theater.harness.contracts.manifest import (
    HarnessManifest,
    HookChannelManifest,
    LaunchManifest,
    ObservationManifest,
    ScreenManifest,
    SourceManifest,
    UnavailableChannelManifest,
)
from theater.harness.loading.models import LoadedPlugin
from theater.harness.registry.diagnostics import project_plugin
from theater.mcp_plugins import registry as mcp_registry
from theater.mcp_plugins.loading import LOCAL, SHIPPED, LoadedMcpPlugin
from theater.mcp_plugins.loading import discover as discover_mcp
from theater.plugins import diagnostics as plugin_diagnostics
from theater.plugins.catalog import UserPluginCatalog, scan
from theater.plugins.diagnostics import describe

local_dir = shared_local_dir  # the shared fixture


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


def test_mcp_selection_matches_install_for_duplicates_and_override(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_registry, "MCP_SERVERS", {})
    monkeypatch.setattr(mcp_registry, "_PLUGINS", {})
    monkeypatch.setattr(mcp_registry, "_DIAGNOSTICS", [])
    shipped_root = tmp_path / "shipped-mcp"
    local_root = tmp_path / "plugins"
    _manifest(shipped_root, "shared", _mcp_manifest().replace("'fixture'", "'shipped'"))
    _manifest(local_root, "shared", _mcp_manifest().replace("'fixture'", "'local'"))
    shipped_duplicates = [
        LoadedMcpPlugin(tmp_path / label, SHIPPED, "shipdup")
        for label in ("shipdup-second", "shipdup-first")
    ]
    local_duplicates = [
        LoadedMcpPlugin(tmp_path / label, LOCAL, "localdup")
        for label in ("localdup-second", "localdup-first")
    ]
    shipped = [*shipped_duplicates, *discover_mcp(shipped_root, source=SHIPPED)]
    local = [*local_duplicates, *scan(local_root).mcp_servers]
    settings = config.Config(mcp=config.McpSection(enabled=["shared", "shipdup", "localdup"]))

    monkeypatch.setattr(mcp_registry, "discover", lambda *_args, **_kwargs: shipped)
    assert mcp_registry.install(settings, shipped_dir=shipped_root, local_plugins=local) == [
        "shared"
    ]
    assert mcp_registry.get("shared").description == "local"
    registry_duplicates = mcp_registry.diagnostics()
    assert [item.path for item in registry_duplicates] == [
        *(plugin.path for plugin in shipped_duplicates),
        *(plugin.path for plugin in local_duplicates),
    ]

    monkeypatch.setattr(plugin_diagnostics, "discover_mcp", lambda *_args, **_kwargs: shipped)
    monkeypatch.setattr(
        plugin_diagnostics,
        "scan_user_plugins",
        lambda *_args, **_kwargs: UserPluginCatalog((), tuple(local), (), ()),
    )
    rows = describe(
        settings,
        local_dir=local_root,
        harness_shipped_dir=tmp_path / "shipped-harnesses",
        mcp_shipped_dir=shipped_root,
    )
    shared = {row["source"]: row for row in rows if row["name"] == "shared"}
    assert shared[SHIPPED]["state"] == "overridden"
    assert shared[LOCAL]["state"] == "loaded"
    assert shared[LOCAL]["description"] == mcp_registry.get("shared").description
    for duplicates in (shipped_duplicates, local_duplicates):
        matching = [row for row in rows if row["name"] == duplicates[0].name]
        assert [Path(row["path"]) for row in matching] == [plugin.path for plugin in duplicates]
        assert all(row["state"] == "duplicate" for row in matching)
        assert [row["error"] for row in matching] == [
            item.error for item in registry_duplicates if item.name == duplicates[0].name
        ]


def test_mcp_diagnostics_rejects_unknown_package_source(tmp_path, monkeypatch):
    unknown = LoadedMcpPlugin(tmp_path / "unknown", "unknown", "unknown")
    monkeypatch.setattr(plugin_diagnostics, "discover_mcp", lambda *_args, **_kwargs: [unknown])

    with pytest.raises(KeyError, match="unknown"):
        describe(
            config.Config(),
            local_dir=tmp_path / "plugins",
            harness_shipped_dir=tmp_path / "shipped-harnesses",
            mcp_shipped_dir=tmp_path / "shipped-mcp",
        )


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


def test_describe_projects_validated_manifest_channels_and_unavailability(local_dir):
    install(local_dir)
    row = {row["name"]: row for row in harness_registry.describe()}["vibe"]

    assert row["manifest_path"].endswith("vibe/manifest.py")
    assert row["approvals"] == ["manual", "edits", "yolo"]
    assert row["primary_channel"] == {
        "id": "transcript",
        "kind": "transcript",
        "availability": "declared",
        "capabilities": [
            {"signal": "identity", "ownership": "primary"},
            {"signal": "content", "ownership": "primary"},
            {"signal": "turn", "ownership": "primary"},
            {"signal": "model", "ownership": "primary"},
            {"signal": "tool", "ownership": "primary"},
            {"signal": "timing", "ownership": "primary"},
            {"signal": "usage", "ownership": "primary"},
            {"signal": "lineage", "ownership": "primary"},
        ],
    }
    unavailable = {
        channel["id"]: channel for channel in row["channels"] if "unavailable_reason" in channel
    }
    assert unavailable["native-hooks"]["kind"] == "hook"
    assert unavailable["native-hooks"]["unavailable_reason"]
    assert unavailable["native-otel"]["protocol"] == "otlp_http_json"
    assert unavailable["native-otel"]["unavailable_reason"]


def test_diagnostics_projection_bounds_untrusted_manifest_output(tmp_path):
    bindings = tuple(
        HookBinding(
            event=f"event-{index}",
            signals=(),
            decoder=lambda _context: (),
            correlation=lambda _context: "native",
        )
        for index in range(HARNESS_DIAGNOSTICS_MAX_BINDINGS + 1)
    )
    hook = HookChannelManifest(
        declaration=ChannelDeclaration(id="hooks", kind=ChannelKind.HOOK),
        bindings=bindings,
    )
    unavailable = UnavailableChannelManifest(
        declaration=ChannelDeclaration(id="unavailable", kind=ChannelKind.OTEL),
        reason="unavailable " * (HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS + 1),
    )
    manifest = HarnessManifest(
        api_version=1,
        binary="acme",
        icon="@",
        launch=LaunchManifest(planner=lambda _context: None, approvals=()),
        observation=ObservationManifest(
            primary=SourceManifest(
                factory=lambda _context: None,
                channel=ChannelDeclaration(id="primary", kind=ChannelKind.TRANSCRIPT),
            ),
            screen=ScreenManifest(classifier=lambda _context: None),
            enrichments=(
                hook,
                unavailable,
                *tuple(
                    ChannelDeclaration(id=f"channel-{index}", kind=ChannelKind.HOOK)
                    for index in range(HARNESS_DIAGNOSTICS_MAX_CHANNELS)
                ),
            ),
        ),
    )
    runtime_channel_ids = (
        "primary",
        "hooks",
        *(f"channel-{index}" for index in range(HARNESS_DIAGNOSTICS_MAX_CHANNELS - 1)),
        "foreign-channel",
    )
    runtime = {
        "acme": {
            f"participant-{index}": tuple(
                ChannelHealth(channel_id=channel_id) for channel_id in runtime_channel_ids
            )
            for index in range(HARNESS_DIAGNOSTICS_MAX_PARTICIPANTS + 1)
        }
    }

    row = project_plugin(
        {"name": "acme"},
        LoadedPlugin(path=tmp_path / "acme", source="local", name="acme", manifest=manifest),
        runtime,
    )

    assert len(row["channels"]) == HARNESS_DIAGNOSTICS_MAX_CHANNELS
    assert row["channels_omitted"] == 3
    projected_hook = next(channel for channel in row["channels"] if channel["id"] == "hooks")
    assert len(projected_hook["bindings"]) == HARNESS_DIAGNOSTICS_MAX_BINDINGS
    assert projected_hook["bindings_omitted"] == 1
    projected_unavailable = next(
        channel for channel in row["channels"] if channel["id"] == "unavailable"
    )
    assert len(projected_unavailable["unavailable_reason"]) <= (
        HARNESS_DIAGNOSTICS_UNAVAILABLE_REASON_MAX_CHARS
    )
    assert "chars omitted" in projected_unavailable["unavailable_reason"]
    runtime_row = row["runtime"]
    assert len(runtime_row["participants"]) == HARNESS_DIAGNOSTICS_MAX_PARTICIPANTS
    assert runtime_row["participants_omitted"] == 1
    assert len(runtime_row["participants"][0]["channels"]) == HARNESS_DIAGNOSTICS_MAX_CHANNELS
    assert runtime_row["participants"][0]["channels_omitted"] == 1
    assert all(
        channel["id"] != "foreign-channel" for channel in runtime_row["participants"][0]["channels"]
    )
