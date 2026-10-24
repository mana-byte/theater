"""Vibe's qualification provisioning points the stock CLI at the mock and trusts its workdir."""

from __future__ import annotations

import tomllib
from dataclasses import replace
from pathlib import Path

from tests.support.shipped import VibeHarness
from theater.harness.contracts.compat import ProvisionContext


def _context(tmp_path: Path, models: tuple[str, ...] = ("mock-a", "mock-b")) -> ProvisionContext:
    return ProvisionContext(
        sandbox_root=tmp_path,
        home=tmp_path / "home",
        config_dir=tmp_path / "home" / ".config",
        workdir=tmp_path / "work",
        mock_urls={"openai-chat": "http://127.0.0.1:9/v1"},
        binary=tmp_path / "vibe",
        version="3.0.2",
        models=models,
    )


def test_provision_serves_every_logical_model_from_the_mock_over_openai(tmp_path: Path) -> None:
    provisioned = VibeHarness().compat.provision(_context(tmp_path))

    config = tomllib.loads(provisioned.files[tmp_path / "home" / ".vibe" / "config.toml"])
    assert provisioned.protocol == "openai-chat"
    assert provisioned.models == ("mock-a", "mock-b")
    assert provisioned.env == {"VIBE_HOME": str(tmp_path / "home" / ".vibe")}
    assert config["active_model"] == "mock-a"
    assert [model["alias"] for model in config["models"]] == ["mock-a", "mock-b"]
    (provider,) = config["providers"]
    assert provider["api_base"] == "http://127.0.0.1:9/v1"
    assert {model["provider"] for model in config["models"]} == {provider["name"]}


def test_provision_trusts_the_resolved_workdir(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "work")

    provisioned = VibeHarness().compat.provision(replace(_context(tmp_path), workdir=link))

    trusted = tomllib.loads(provisioned.files[tmp_path / "home" / ".vibe" / "trusted_folders.toml"])
    assert trusted["trusted"] == [str((tmp_path / "work").resolve())]


def test_provision_falls_back_to_two_logical_models(tmp_path: Path) -> None:
    provisioned = VibeHarness().compat.provision(_context(tmp_path, models=()))

    assert provisioned.models == ("mock-a", "mock-b")
