"""Pi's qualification provisioning points the stock CLI at the mock over OpenAI completions."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from theater.harness.builtin.plugins.pi.compat import COMPAT
from theater.harness.builtin.plugins.pi.manifest import MANIFEST
from theater.harness.contracts.compat import ProvisionContext


def _context(tmp_path: Path, models: tuple[str, ...] = ("mock-a", "mock-b")) -> ProvisionContext:
    return ProvisionContext(
        sandbox_root=tmp_path,
        home=tmp_path / "home",
        config_dir=tmp_path / "home" / ".config",
        workdir=tmp_path / "work",
        mock_urls={"openai-chat": "http://127.0.0.1:9/v1"},
        binary=tmp_path / "pi",
        version="0.84.4",
        models=models,
    )


def _agent_dir(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".pi" / "agent"


def test_manifest_wires_the_compat_manifest() -> None:
    assert MANIFEST.compat is COMPAT
    assert COMPAT.native_edit is None  # Pi declares only the yolo approval.
    assert COMPAT.mcp_call is None  # The bridge offers the probe tool under its own name.


def test_provision_serves_every_logical_model_from_the_mock_over_openai(tmp_path: Path) -> None:
    provisioned = COMPAT.provision(_context(tmp_path))
    agent_dir = _agent_dir(tmp_path)

    assert provisioned.protocol == "openai-chat"
    assert provisioned.models == ("mock-a", "mock-b")
    assert provisioned.env == {"PI_CODING_AGENT_DIR": str(agent_dir)}

    catalog = json.loads(provisioned.files[agent_dir / "models.json"])
    (provider,) = catalog["providers"].values()
    assert provider["baseUrl"] == "http://127.0.0.1:9/v1"
    assert provider["api"] == "openai-completions"
    assert provider["apiKey"]  # a literal credential keeps the models selectable without /login
    assert [model["id"] for model in provider["models"]] == ["mock-a", "mock-b"]


def test_provision_starts_on_the_mock_provider_and_silences_startup(tmp_path: Path) -> None:
    provisioned = COMPAT.provision(_context(tmp_path))
    agent_dir = _agent_dir(tmp_path)

    settings = json.loads(provisioned.files[agent_dir / "settings.json"])
    (provider_name,) = json.loads(provisioned.files[agent_dir / "models.json"])["providers"]
    assert settings["defaultProvider"] == provider_name
    assert settings["defaultModel"] == "mock-a"
    assert settings["defaultProjectTrust"] == "always"
    assert settings["enableInstallTelemetry"] is False
    assert settings["enableAnalytics"] is False


def test_provision_trusts_the_resolved_workdir(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "work")

    provisioned = COMPAT.provision(replace(_context(tmp_path), workdir=link))

    trust = json.loads(provisioned.files[_agent_dir(tmp_path) / "trust.json"])
    assert trust == {str((tmp_path / "work").resolve()): True}


def test_provision_falls_back_to_two_logical_models(tmp_path: Path) -> None:
    provisioned = COMPAT.provision(_context(tmp_path, models=()))

    assert provisioned.models == ("mock-a", "mock-b")
    catalog = json.loads(provisioned.files[_agent_dir(tmp_path) / "models.json"])
    (provider,) = catalog["providers"].values()
    assert [model["id"] for model in provider["models"]] == ["mock-a", "mock-b"]
