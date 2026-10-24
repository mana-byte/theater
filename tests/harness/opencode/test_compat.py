"""OpenCode's qualification provisioning points the supported CLI at the mock."""

from __future__ import annotations

import json
from pathlib import Path

from tests.support.shipped import OpenCodeHarness
from theater.harness.builtin.plugins.opencode.manifest import MANIFEST, manifest_for_paths
from theater.harness.contracts.compat import ProvisionContext


def _context(
    tmp_path: Path, *, version: str = "2.0.18", models: tuple[str, ...] = ("mock-a", "mock-b")
) -> ProvisionContext:
    return ProvisionContext(
        sandbox_root=tmp_path,
        home=tmp_path / "home",
        config_dir=tmp_path / "home" / ".config",
        workdir=tmp_path / "work",
        mock_urls={"openai-chat": "http://127.0.0.1:9/v1"},
        binary=tmp_path / "opencode",
        version=version,
        models=models,
    )


def test_provision_serves_every_logical_model_from_the_mock_over_openai(
    tmp_path: Path,
) -> None:
    provisioned = OpenCodeHarness().compat.provision(_context(tmp_path))

    config = json.loads(
        provisioned.files[tmp_path / "home" / ".config" / "opencode" / "opencode.json"]
    )
    (provider,) = config["provider"].values()
    assert provisioned.protocol == "openai-chat"
    assert provisioned.models == ("mockprov/mock-a", "mockprov/mock-b")
    assert provider["options"]["baseURL"] == "http://127.0.0.1:9/v1"
    assert set(provider["models"]) == {"mock-a", "mock-b"}
    assert config["model"] == "mockprov/mock-a"


def test_provision_keeps_the_cli_off_the_network(tmp_path: Path) -> None:
    provisioned = OpenCodeHarness().compat.provision(_context(tmp_path))

    config = json.loads(next(iter(provisioned.files.values())))
    assert provisioned.env == {
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
    }
    assert config["autoupdate"] is False
    assert config["share"] == "disabled"
    assert "agent" not in config


def test_provision_leaves_the_launch_owned_config_env_alone(tmp_path: Path) -> None:
    provisioned = OpenCodeHarness().compat.provision(_context(tmp_path))

    assert "OPENCODE_CONFIG" not in provisioned.env
    assert "OPENCODE_DB" not in provisioned.env


def test_provision_falls_back_to_two_logical_models(tmp_path: Path) -> None:
    provisioned = OpenCodeHarness().compat.provision(_context(tmp_path, models=()))

    assert provisioned.models == ("mockprov/mock-a", "mockprov/mock-b")


def test_native_edit_names_the_target_as_the_2x_write_tool_declares_it() -> None:
    edit = OpenCodeHarness().compat.native_edit

    assert edit is not None
    assert edit.name == "write"
    assert edit.render(path="/w/x.txt", nonce="N1") == {
        "path": "/w/x.txt",
        "content": "N1\n",
    }


def test_every_manifest_the_plugin_builds_carries_the_compat_declaration(
    tmp_path: Path,
) -> None:
    built = manifest_for_paths(db=tmp_path / "db")

    assert MANIFEST.compat is not None
    assert built.compat is MANIFEST.compat
