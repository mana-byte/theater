"""Small contract checks for the Claude release adapter."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scripts.harness_compat.adapters.claude import ADAPTER, changelog_hints, seed_config
from scripts.harness_compat.contract import Check, InstallFailed
from scripts.harness_compat.sandbox import make_sandbox


def test_claude_evidence_only_bump_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "theater/harness/builtin/plugins/claude/compatibility.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def probe_claude_hooks(context):\n"
        "    return _probe_claude(context, floor=(2, 1, 119), policy='p', upper=(3, 0, 0))\n"
    )
    assert ADAPTER.verified(tmp_path).floor == "2.1.119"
    assert ADAPTER.verified(tmp_path).ceiling == "3.0.0"
    changed = ADAPTER.apply_bump(tmp_path, ["3.0.0"])
    assert changed == [Path("tests/fixtures/harness_compat/claude/3.0.0.json")]
    assert ADAPTER.apply_bump(tmp_path, ["3.0.0"]) == []
    evidence = json.loads((tmp_path / changed[0]).read_text())
    assert evidence["harness"] == "claude" and evidence["version"] == "3.0.0"
    assert ADAPTER.verified(tmp_path).evidenced == frozenset({"3.0.0"})
    assert source.read_text().count("upper=(3, 0, 0)") == 1


def test_claude_seed_config_uses_realpath(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    config = tmp_path / "config"
    seed_config(config, alias, "2.1.296")
    state = json.loads((config / ".claude.json").read_text())
    assert state["projects"] == {str(actual.resolve()): {"hasTrustDialogAccepted": True}}
    assert state["lastOnboardingVersion"] == "2.1.296"
    assert json.loads((config / "settings.json").read_text()) == {"theme": "dark"}


def test_claude_changelog_hints_only_selected_release() -> None:
    changelog = "\n".join(
        [
            "## 2.1.297",
            "- Fixed a transcript bug in a later release",
            "## 2.1.296",
            "- Fixed SessionStart hooks after resume",
            "- Improved terminal colors",
            "- Fixed --session-id replay",
            "## 2.1.295",
            "- Fixed turn_duration marker",
        ]
    )
    assert changelog_hints("2.1.296", changelog) == (
        "- Fixed SessionStart hooks after resume",
        "- Fixed --session-id replay",
    )


def test_claude_fake_install_failure_is_inconclusive(tmp_path: Path, monkeypatch) -> None:
    def fail(*args, **kwargs):
        raise OSError("npm unavailable")

    monkeypatch.setattr("scripts.harness_compat.adapters.claude.subprocess.run", fail)
    with (
        make_sandbox("claude", root=tmp_path) as sb,
        pytest.raises(InstallFailed, match="npm unavailable"),
    ):
        ADAPTER.install(sb, "2.1.296")


@pytest.mark.parametrize(("unexpected_route", "verdict"), [(False, "red"), (True, "tooling")])
def test_claude_qualify_calls_framework(
    tmp_path: Path, monkeypatch, unexpected_route: bool, verdict: str
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(root, nodeids, **kwargs):
        calls.append(tuple(nodeids))
        status = "pass" if tuple(nodeids) == ADAPTER.consistency_tests else "fail"
        return tuple(Check(nodeid, status, "proof failed") for nodeid in nodeids)

    monkeypatch.setattr("scripts.harness_compat.adapters.claude.framework.run_pytest", fake_run)
    monkeypatch.setattr("scripts.harness_compat.adapters.claude.github.raw_file", lambda *_: "")
    with make_sandbox("claude", root=tmp_path / "sandbox") as sb:
        if unexpected_route:
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(sb.mock_base_urls["anthropic"] + "/unexpected")
        result = ADAPTER.qualify(tmp_path, "2.1.296", sb, Path("/fake/claude"))
    assert calls == [ADAPTER.green_tests[:2], ADAPTER.consistency_tests]
    assert result.verdict == verdict
    assert result.checks[2].status == "skip"
