"""Focused contracts for the Wave 1 MCP-server plugin foundation."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from theater import config, harness, paths
from theater.mcp_plugins import (
    MANIFEST_API_VERSION,
    CompiledMcpPlugin,
    McpConfigField,
    McpConfigKind,
    McpConfigResolutionError,
    McpConfigSchema,
    McpLaunchContext,
    McpLaunchManifest,
    McpLaunchPlan,
    McpManifestValidationError,
    McpPluginError,
    McpServerManifest,
    McpServerSpec,
    PluginCapability,
    SecretValue,
    compile_manifest,
    registry,
    validate_manifest,
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
    skills={skills!r},
    config=McpConfigSchema({{
        "endpoint": McpConfigField(McpConfigKind.STRING, default="https://default.invalid"),
        "token": McpConfigField(McpConfigKind.SECRET, required=True),
    }}),
)
"""


def write_plugin(
    root: Path,
    name: str = "acme",
    *,
    command: str = "acme-server",
    skills: tuple[str, ...] = (),
) -> Path:
    package = root / name
    package.mkdir(parents=True)
    manifest = package / "manifest.py"
    manifest.write_text(MANIFEST.format(command=command, skills=skills), encoding="utf-8")
    return manifest


def manifest_with_config(schema: McpConfigSchema) -> McpServerManifest:
    return McpServerManifest(
        api_version=MANIFEST_API_VERSION,
        description="Table-list test sidecar",
        capabilities=frozenset({PluginCapability.PARTICIPANTS_READ}),
        launch=McpLaunchManifest(planner=lambda _context: McpLaunchPlan(command="table-list")),
        config=schema,
    )


def table_list_schema(*, max_items: int | None = 2) -> McpConfigSchema:
    return McpConfigSchema(
        {
            "channels": McpConfigField(
                McpConfigKind.TABLE_LIST,
                min_items=1,
                max_items=max_items,
                item_schema=McpConfigSchema(
                    {
                        "folder_uid": McpConfigField(McpConfigKind.STRING, required=True),
                        "label": McpConfigField(McpConfigKind.STRING, default="Inbox"),
                        "token": McpConfigField(McpConfigKind.SECRET, required=True),
                    }
                ),
            )
        }
    )


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
    local = tmp_path / "plugins"
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


def test_invalid_declared_skill_omits_the_local_plugin(tmp_path, monkeypatch):
    local = tmp_path / "plugins"
    write_plugin(local, skills=("missing-skill",))
    monkeypatch.setenv("ACME_TEST_TOKEN", "secret")
    settings = config.Config(
        mcp=config.McpSection(
            enabled=["acme"],
            plugins={"acme": {"token": {"env": "ACME_TEST_TOKEN"}}},
        )
    )

    assert registry.install(settings, local_dir=local, shipped_dir=tmp_path / "shipped") == []
    assert "MCP-plugin skill root" in registry.diagnostics()[0].error


def test_table_lists_resolve_nested_values_immutably_and_report_indexed_paths(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CHANNEL_TOKEN", "channel-secret")
    values = {"channels": [{"folder_uid": "inbox", "token": {"env": "CHANNEL_TOKEN"}}]}
    plugin = compile_manifest("acme", manifest_with_config(table_list_schema()), values)
    channels = plugin.config["channels"]

    assert McpConfigKind.TABLE_LIST.value == "list[table]"
    assert isinstance(channels, tuple)
    assert isinstance(channels[0], Mapping)
    assert channels[0]["label"] == "Inbox"
    assert isinstance(channels[0]["token"], SecretValue)
    assert "channel-secret" not in repr(plugin.config)
    with pytest.raises(TypeError):
        channels[0]["label"] = "Other"  # type: ignore[index]

    context = McpLaunchContext(
        participant_id="p-1",
        cwd=tmp_path,
        config=plugin.config,
        state_dir=tmp_path / "state",
    )
    with pytest.raises(TypeError):
        context.config["channels"][0]["label"] = "Other"  # type: ignore[index]
    with pytest.raises(McpConfigResolutionError) as error:
        compile_manifest(
            "acme",
            manifest_with_config(table_list_schema()),
            {"channels": [values["channels"][0], {"token": {"env": "CHANNEL_TOKEN"}}]},
        )
    assert error.value.field == "channels[1].folder_uid"


@pytest.mark.parametrize(
    "channels, field",
    [
        ("not-a-list", "channels"),
        (["not-a-table"], "channels[0]"),
        ([], "channels"),
        (
            [
                {"folder_uid": "one", "token": {"env": "CHANNEL_TOKEN"}},
                {"folder_uid": "two", "token": {"env": "CHANNEL_TOKEN"}},
            ],
            "channels",
        ),
    ],
)
def test_table_lists_require_table_items_and_honor_bounds(channels, field):
    with pytest.raises(McpConfigResolutionError) as error:
        compile_manifest(
            "acme",
            manifest_with_config(table_list_schema(max_items=1)),
            {"channels": channels},
        )
    assert error.value.field == field


def test_table_list_schemas_require_valid_acyclic_item_schemas():
    for item_schema in (None, object()):
        schema = McpConfigSchema(
            {"channels": McpConfigField(McpConfigKind.TABLE_LIST, item_schema=item_schema)}
        )
        with pytest.raises(McpManifestValidationError):
            validate_manifest("acme", manifest_with_config(schema))

    cyclic = McpConfigSchema()
    object.__setattr__(
        cyclic,
        "fields",
        {"again": McpConfigField(McpConfigKind.TABLE_LIST, item_schema=cyclic)},
    )
    schema = McpConfigSchema(
        {"channels": McpConfigField(McpConfigKind.TABLE_LIST, item_schema=cyclic)}
    )

    with pytest.raises(McpManifestValidationError, match="cyclic"):
        validate_manifest("acme", manifest_with_config(schema))


def test_user_plugins_share_one_root(theater_home):
    assert paths.plugins_dir() == theater_home / "plugins"
    assert paths.plugins_dir().is_dir()


def test_harness_registry_installs_enabled_mcp_servers_at_startup(tmp_path, monkeypatch):
    local = tmp_path / "plugins"
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
            local_dir=local,
            shipped_dir=tmp_path / "shipped-harnesses",
            mcp_shipped_dir=tmp_path / "shipped-mcp",
        )
        == []
    )
    assert registry.get("acme").name == "acme"


def test_disabled_broken_package_is_omitted_without_a_diagnostic(tmp_path):
    local = tmp_path / "plugins"
    package = local / "bomb"
    package.mkdir(parents=True)
    (package / "manifest.py").write_text("raise RuntimeError('must not import')", encoding="utf-8")

    loaded = config.Config()
    assert registry.install(loaded, local_dir=local, shipped_dir=tmp_path / "shipped") == []
    assert registry.diagnostics() == ()


def test_disabled_package_still_reserves_the_global_canonical_name(tmp_path):
    local = tmp_path / "plugins"
    write_plugin(local, "vibe")

    with pytest.raises(config.ConfigError, match="conflicts with harness"):
        harness.install(
            config.Config(),
            local_dir=local,
            mcp_shipped_dir=tmp_path / "shipped-mcp",
        )


def test_local_package_overrides_shipped_package_of_the_same_kind(tmp_path, monkeypatch):
    local = tmp_path / "plugins"
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
    root = tmp_path / "plugins"
    package = root / "wrong"
    package.mkdir(parents=True)
    manifest_path = package / "manifest.py"
    manifest_path.write_text("MANIFEST = object()\n", encoding="utf-8")

    result = load_plugin(discover(root, source=LOCAL)[0])
    assert result.error is not None
    assert str(manifest_path) in result.error
    assert "McpServerManifest" in result.error


def test_unknown_enabled_and_broken_local_packages_are_diagnostics_not_config_errors(tmp_path):
    local = tmp_path / "plugins"
    (local / "broken").mkdir(parents=True)
    settings = config.Config(mcp=config.McpSection(enabled=["broken", "missing"]))

    assert registry.install(settings, local_dir=local, shipped_dir=tmp_path / "shipped") == []
    messages = {item.name: item.error for item in registry.diagnostics()}
    assert "manifest.py" in messages["broken"]
    assert "not found" in messages["missing"]


@pytest.mark.parametrize("name", ("theater", "theater_wait"))
def test_enabled_plugin_cannot_claim_a_reserved_core_server_name(tmp_path, monkeypatch, name):
    local = tmp_path / "plugins"
    write_plugin(local, name)
    monkeypatch.setenv("ACME_TEST_TOKEN", "secret")
    settings = config.Config(
        mcp=config.McpSection(
            enabled=[name],
            plugins={name: {"token": {"env": "ACME_TEST_TOKEN"}}},
        )
    )

    assert registry.install(settings, local_dir=local, shipped_dir=tmp_path / "shipped") == []
    assert {item.name for item in registry.diagnostics()} == {name}
    assert "reserved by Theater" in registry.diagnostics()[0].error


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
            local_dir=tmp_path / "plugins",
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
    assert manifest.skills == ()
    assert PluginCapability.SESSIONS_KILL.value == "sessions.kill"


@pytest.mark.parametrize("skills", [("Not-Canonical",), ("same", "same")])
def test_manifest_rejects_invalid_skill_declarations(skills):
    manifest = McpServerManifest(
        api_version=MANIFEST_API_VERSION,
        description="Acme",
        capabilities=frozenset({PluginCapability.JOBS_READ}),
        launch=McpLaunchManifest(planner=lambda _context: McpLaunchPlan(command="acme")),
        skills=skills,
    )

    with pytest.raises(McpManifestValidationError, match="skills"):
        validate_manifest("acme", manifest)
