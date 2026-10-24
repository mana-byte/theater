"""Codex's qualification provisioning points the stock CLI at the mock and trusts its workdir."""

from __future__ import annotations

import json
import tomllib
from dataclasses import replace
from pathlib import Path

from tests.support.shipped import CodexHarness
from theater.harness.contracts.compat import ProvisionContext


def _context(tmp_path: Path, models: tuple[str, ...] = ("mock-a", "mock-b")) -> ProvisionContext:
    return ProvisionContext(
        sandbox_root=tmp_path,
        home=tmp_path / "home",
        config_dir=tmp_path / "home" / ".config",
        workdir=tmp_path / "work",
        mock_urls={"openai-responses": "http://127.0.0.1:9/v1"},
        binary=tmp_path / "codex",
        version="0.163.0",
        models=models,
    )


def test_provision_serves_every_logical_model_from_the_mock_over_responses(tmp_path: Path) -> None:
    provisioned = CodexHarness().compat.provision(_context(tmp_path))

    home = tmp_path / "home" / ".codex"
    config = tomllib.loads(provisioned.files[home / "config.toml"])
    assert provisioned.protocol == "openai-responses"
    assert provisioned.models == ("mock-a", "mock-b")
    assert provisioned.env == {"CODEX_HOME": str(home)}
    assert config["model"] == "mock-a"
    provider = config["model_providers"][config["model_provider"]]
    assert (provider["base_url"], provider["wire_api"]) == ("http://127.0.0.1:9/v1", "responses")
    assert config["check_for_update_on_startup"] is False


def test_provision_catalogs_each_model_with_the_freeform_edit_tool(tmp_path: Path) -> None:
    provisioned = CodexHarness().compat.provision(_context(tmp_path))

    home = tmp_path / "home" / ".codex"
    config = tomllib.loads(provisioned.files[home / "config.toml"])
    catalog = json.loads(provisioned.files[Path(config["model_catalog_json"])])
    assert config["model_catalog_json"] == str(home / "catalog.json")
    assert [model["slug"] for model in catalog["models"]] == ["mock-a", "mock-b"]
    assert {model["apply_patch_tool_type"] for model in catalog["models"]} == {"freeform"}


def test_provision_trusts_the_resolved_workdir(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "work")

    provisioned = CodexHarness().compat.provision(replace(_context(tmp_path), workdir=link))

    config = tomllib.loads(provisioned.files[tmp_path / "home" / ".codex" / "config.toml"])
    assert config["projects"] == {str((tmp_path / "work").resolve()): {"trust_level": "trusted"}}


def test_provision_falls_back_to_two_logical_models(tmp_path: Path) -> None:
    provisioned = CodexHarness().compat.provision(_context(tmp_path, models=()))

    assert provisioned.models == ("mock-a", "mock-b")


def test_native_edit_is_an_apply_patch_that_adds_the_scratch_file() -> None:
    edit = CodexHarness().compat.native_edit

    assert edit is not None and edit.name == "apply_patch"
    assert edit.render(path="/work/EDIT-1.txt", nonce="EDIT-1") == (
        "*** Begin Patch\n*** Add File: /work/EDIT-1.txt\n+EDIT-1\n*** End Patch\n"
    )
