"""The launch-local Claude native plugin and its private descriptor."""

from __future__ import annotations

import json
from pathlib import Path

from theater import paths
from theater.harness.builtin.plugins.claude.frontend import install_claude_frontend
from theater.harness.builtin.plugins.claude.runtime_protocol import PROTOCOL
from theater.harness.contracts.runtime import RuntimeFrontendInstallContext


def test_descriptor_points_the_journal_at_the_participant_observation_dir(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "home"))
    context = RuntimeFrontendInstallContext(
        participant_id="p1",
        endpoint="unix://" + str(tmp_path / "frontend.sock"),
        token_file=tmp_path / "token",
    )
    overlay = install_claude_frontend(context)
    root = paths.participant_observation_dir("p1", "claude")
    descriptor = json.loads(overlay.files[root / "native-frontend.json"])
    assert descriptor["protocol"] == PROTOCOL
    assert descriptor["journal"] == str(root / "native-journal.json")
    assert overlay.env["THEATER_CLAUDE_FRONTEND_CONFIG"] == str(root / "native-frontend.json")
    assert overlay.args == ("--plugin-dir", str(root / "native-plugin"))
    assert set(overlay.files) == {
        root / "native-plugin" / ".claude-plugin" / "plugin.json",
        root / "native-plugin" / "hooks" / "hooks.json",
        root / "native-plugin" / "hooks" / "register.ts",
        root / "native-frontend.json",
    }
