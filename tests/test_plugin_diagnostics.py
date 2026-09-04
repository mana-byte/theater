"""Local cross-kind package diagnostics."""

from __future__ import annotations

import json
from pathlib import Path

from theater import cli, config
from theater.cli.commands import introspection
from theater.plugins.diagnostics import describe

FIXTURES = Path(__file__).parent / "fixtures" / "mcp_plugins"


def _manifest(root: Path, name: str, body: str) -> None:
    package = root / name
    package.mkdir(parents=True)
    (package / "manifest.py").write_text(body, encoding="utf-8")


def test_projection_is_deterministic_and_redacts_disabled_import_failures(tmp_path):
    local_mcp = tmp_path / "mcp_servers"
    local_harness = tmp_path / "harnesses"
    _manifest(local_mcp, "bomb", "raise RuntimeError('this-is-a-secret')\n")
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
        mcp=config.McpSection(enabled=["native", "missing", "wrongmcp"]),
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
    assert by_key[("mcp_server", "compat")]["state"] == "disabled"
    assert by_key[("mcp_server", "bomb")]["state"] == "disabled"
    assert by_key[("mcp_server", "missing")]["state"] == "missing"
    wrong_mcp = by_key[("mcp_server", "wrongmcp")]
    assert wrong_mcp["state"] == "broken"
    assert "HarnessManifest" in wrong_mcp["error"]
    wrong = by_key[("harness", "wrong")]
    assert wrong["state"] == "broken"
    assert "McpServerManifest" in wrong["error"]
    assert "this-is-a-secret" not in json.dumps(rows)


def test_plugins_command_is_local_and_has_deterministic_json(monkeypatch, capsys):
    rows = [
        {
            "kind": "mcp_server",
            "name": "acme",
            "source": "local",
            "enabled": True,
            "state": "loaded",
            "path": "/tmp/acme",
            "manifest_path": "/tmp/acme/manifest.py",
            "error": None,
            "description": "Acme",
            "capabilities": ["participants.read"],
        }
    ]
    monkeypatch.setattr(introspection, "describe_plugins", lambda _config: rows)
    monkeypatch.setattr(
        cli.harness_registry,
        "install",
        lambda _config: (_ for _ in ()).throw(AssertionError()),
    )
    assert cli.main(["plugins", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == rows
