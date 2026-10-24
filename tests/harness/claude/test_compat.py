"""Claude's qualification provisioning isolates its profile and pre-answers consent dialogs."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.shipped import ClaudeCodeHarness
from theater.harness.contracts.compat import ProvisionContext


def _context(tmp_path: Path, models: tuple[str, ...] = ()) -> ProvisionContext:
    return ProvisionContext(
        sandbox_root=tmp_path,
        home=tmp_path / "home",
        config_dir=tmp_path / "home" / ".config",
        workdir=tmp_path / "work",
        mock_urls={"anthropic": "http://127.0.0.1:9"},
        binary=tmp_path / "claude",
        version="2.1.296",
        models=models,
    )


@pytest.mark.parametrize("models", [(), ("mock-a", "mock-b"), ("custom-a", "custom-b")])
def test_provision_routes_logical_models_to_messages_without_writing(
    tmp_path: Path, models: tuple[str, ...]
) -> None:
    provisioned = ClaudeCodeHarness().compat.provision(_context(tmp_path, models))

    assert provisioned.protocol == "anthropic"
    assert provisioned.models == (models or ("mock-a", "mock-b"))
    assert provisioned.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9"
    assert provisioned.env["ANTHROPIC_MODEL"] == provisioned.models[0]
    assert provisioned.env["ANTHROPIC_API_KEY"] == "compat-dummy-key"
    assert provisioned.env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "home" / ".claude")
    assert all(path.is_relative_to(tmp_path) for path in provisioned.files)
    assert not any(tmp_path.iterdir())


def test_provision_pre_answers_onboarding_and_consent_without_overriding_policy(
    tmp_path: Path,
) -> None:
    provisioned = ClaudeCodeHarness().compat.provision(_context(tmp_path))
    home = tmp_path / "home"
    state = json.loads(provisioned.files[home / ".claude" / ".claude.json"])

    assert state["hasCompletedOnboarding"] is True
    assert state["lastOnboardingVersion"] == "2.1.296"
    assert state["theme"] == "dark"
    assert state["customApiKeyResponses"] == {"approved": ["compat-dummy-key"], "rejected": []}
    assert state["bypassPermissionsModeAccepted"] is True
    assert (
        provisioned.files[home / ".claude.json"]
        == provisioned.files[home / ".claude" / ".claude.json"]
    )
    assert json.loads(provisioned.files[home / ".claude" / "settings.json"]) == {
        "permissions": {"skipDangerousModePermissionPrompt": True}
    }
    for opt_out in (
        "DISABLE_AUTOUPDATER",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
    ):
        assert provisioned.env[opt_out] == "1"


def test_provision_trusts_the_resolved_workdir_and_survives_root_binding(tmp_path: Path) -> None:
    (tmp_path / "work").mkdir()
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "work")
    harness = ClaudeCodeHarness(tmp_path / "transcripts")

    provisioned = harness.compat.provision(replace(_context(tmp_path), workdir=link))

    state = json.loads(provisioned.files[tmp_path / "home" / ".claude" / ".claude.json"])
    assert state["projects"] == {
        str((tmp_path / "work").resolve()): {"hasTrustDialogAccepted": True}
    }


def test_native_edit_uses_the_offered_write_schema(tmp_path: Path) -> None:
    compat = ClaudeCodeHarness().compat
    assert compat.native_edit is not None
    assert compat.native_edit.name == "Write"
    assert compat.native_edit.render(path=str(tmp_path / "edit.txt"), nonce="EDIT-proof") == {
        "file_path": str(tmp_path / "edit.txt"),
        "content": "EDIT-proof\n",
    }
    assert compat.mcp_call is None
