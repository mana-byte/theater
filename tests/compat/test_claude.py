"""Small contract checks for the Claude release adapter."""

from __future__ import annotations

import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scripts.harness_compat.adapters.claude import ADAPTER, changelog_hints, seed_config
from scripts.harness_compat.contract import Check, InstallFailed
from scripts.harness_compat.sandbox import make_sandbox
from tests.support.paths import REPO_ROOT
from theater.harness.builtin.plugins.claude.compatibility import _HOOK_FLOOR, _HOOK_UPPER


def _version(parts: tuple[int, int, int]) -> str:
    return ".".join(map(str, parts))


def test_claude_name_policy_and_optional_upper(tmp_path: Path) -> None:
    source = tmp_path / "theater/harness/builtin/plugins/claude/compatibility.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def probe_claude_hooks(context):\n"
        f"    return _probe_claude(context, floor={_HOOK_FLOOR!r}, policy=_POLICY)\n"
    )
    assert ADAPTER.verified(tmp_path).floor == _version(_HOOK_FLOOR)
    assert ADAPTER.verified(tmp_path).ceiling is None


def test_claude_real_gate_and_noop_bump(tmp_path: Path) -> None:
    real_source = REPO_ROOT / "theater/harness/builtin/plugins/claude/compatibility.py"
    source = tmp_path / "theater/harness/builtin/plugins/claude/compatibility.py"
    source.parent.mkdir(parents=True)
    shutil.copy2(real_source, source)
    gate = ADAPTER.verified(tmp_path)
    assert gate.floor == _version(_HOOK_FLOOR) and gate.ceiling == _version(_HOOK_UPPER)
    assert ADAPTER.version_line("2.1.296") == "2.1.296 (Claude Code)"
    assert ADAPTER.apply_bump(tmp_path, [_version(_HOOK_UPPER)]) == []
    assert ADAPTER.apply_bump(tmp_path, [_version(_HOOK_UPPER)]) == []
    assert ADAPTER.verified(tmp_path).evidenced == frozenset()
    assert source.read_bytes() == real_source.read_bytes()
    assert list(tmp_path.rglob("*.json")) == []


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
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        calls.append(tuple(nodeids))
        status = "pass" if tuple(nodeids) == ADAPTER.consistency_tests else "fail"
        return tuple(Check(nodeid, status, "proof failed") for nodeid in nodeids)

    monkeypatch.setattr("scripts.harness_compat.adapters.claude.framework.run_pytest", fake_run)
    monkeypatch.setattr(
        "scripts.harness_compat.adapters.claude.github.raw_file",
        lambda *_: "## 2.1.296\n- Fixed transcript recovery",
    )
    with make_sandbox("claude", root=tmp_path / "sandbox") as sb:
        if unexpected_route:
            with pytest.raises(urllib.error.HTTPError):
                urllib.request.urlopen(sb.mock_base_urls["anthropic"] + "/unexpected")
        result = ADAPTER.qualify(tmp_path, "2.1.296", sb, Path("/fake/claude"))
    assert calls == [ADAPTER.consistency_tests, ADAPTER.green_tests[:2], ADAPTER.green_tests[2:]]
    assert result.verdict == verdict
    assert result.checks[2].status == "fail"
    assert result.checks[2].detail.endswith("- Fixed transcript recovery")


def test_claude_qualify_stops_after_consistency_failure(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(root, nodeids, **kwargs):
        del root, kwargs
        calls.append(tuple(nodeids))
        assert tuple(nodeids) == ADAPTER.consistency_tests
        return (Check(nodeids[0], "fail", "broken bump"),)

    monkeypatch.setattr("scripts.harness_compat.adapters.claude.framework.run_pytest", fake_run)
    with make_sandbox("claude", root=tmp_path / "sandbox") as sb:
        result = ADAPTER.qualify(tmp_path, "2.1.296", sb, Path("/fake/claude"))
    assert calls == [ADAPTER.consistency_tests]
    assert result.verdict == "tooling"
    assert result.checks == (Check(ADAPTER.consistency_tests[0], "fail", "broken bump"),)
