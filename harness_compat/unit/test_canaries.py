"""The canaries command: the policy's newest release, verified, and no silent pass."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from harness_compat import canaries
from harness_compat.codecopy import code_copy
from harness_compat.contract import InstallFailed, Sandbox
from harness_compat.framework import python_path
from harness_compat.policy import load_policy, write_policy
from harness_compat.process import run_group
from harness_compat.sandbox import make_sandbox
from harness_compat.support.mutations import MUTATIONS
from tests.support.paths import REPO_ROOT
from theater.harness.contracts.version_policy import VersionPolicy


class _Adapter:
    name = "claude"

    def __init__(self, report: str = "1.2.0") -> None:
        self.report = report
        self.installed: list[str] = []

    def discover(self) -> list[str]:
        raise AssertionError("the canaries never discover releases")

    def install(self, sb: Sandbox, version: str) -> Path:
        self.installed.append(version)
        script = sb.bin_dir / "claude"
        script.write_text(f"#!/bin/sh\necho {self.report}\n")
        script.chmod(0o755)
        return script


def _pytest(monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str) -> list[dict]:
    seen: list[dict] = []

    def fake(argv, *, cwd, env, timeout):
        seen.append(
            {
                "argv": argv,
                "env": env,
                "cwd": cwd,
                "policy": load_policy(cwd, "claude"),
                "files": sorted(p.name for p in cwd.iterdir()),
            }
        )
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(canaries, "run_group", fake)
    return seen


def test_the_policys_maximum_is_installed_and_every_caught_canary_passes(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _pytest(monkeypatch, 0, "4 passed, 2 skipped in 61s")
    adapter = _Adapter()

    check = canaries.run_canaries(repo, adapter)  # type: ignore[arg-type]

    assert adapter.installed == ["1.2.0"]
    assert (check.status, check.detail) == ("pass", "claude 1.2.0: 4 passed, 2 skipped")
    env = seen[0]["env"]
    assert env[canaries.ENV_ENABLE] == "1" and env[canaries.ENV_HARNESS] == "claude"
    assert env[canaries.ENV_BINARY].endswith("claude")
    assert "-k" not in seen[0]["argv"] and seen[0]["argv"][-1] == canaries.CANARIES_TARGET


def test_canary_collection_selects_exactly_pi_without_matching_the_skipif_marker() -> None:
    with make_sandbox("canary-collection") as sb, code_copy(REPO_ROOT) as tree:
        result = run_group(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--collect-only",
                canaries.CANARIES_TARGET,
            ],
            cwd=tree,
            env={
                **sb.offline_env,
                canaries.ENV_HARNESS: "pi",
                "PYTHONPATH": python_path(tree),
            },
            timeout=30,
        )
    collected = [line for line in result.stdout.splitlines() if "::test_" in line]
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(collected) == len(MUTATIONS) and all("[pi-" in line for line in collected)


def test_an_excluded_max_uses_the_newest_admitted_release(repo: Path, monkeypatch) -> None:
    _pytest(monkeypatch, 0, "4 passed")
    write_policy(repo, "claude", VersionPolicy("1.0.0", "1.2.0", ("1.2.0",)))
    adapter = _Adapter("1.1.0")
    monkeypatch.setattr(adapter, "discover", lambda: ["1.0.0", "1.1.0", "1.2.0", "1.3.0"])
    assert canaries.run_canaries(repo, adapter).status == "pass"
    assert adapter.installed == ["1.1.0"]


def test_no_admitted_release_cannot_be_used_as_a_canary_baseline(repo: Path, monkeypatch) -> None:
    write_policy(repo, "claude", VersionPolicy("1.2.0", "1.2.0", ("1.2.0",)))
    adapter = _Adapter()
    monkeypatch.setattr(adapter, "discover", lambda: ["1.2.0", "1.3.0"])
    with pytest.raises(ValueError, match="no admitted release"):
        canaries.run_canaries(repo, adapter)
    assert not adapter.installed


def test_a_canary_the_suite_missed_is_a_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pytest(monkeypatch, 1, "FAILED test_canaries.py::test[manual-as-yolo-claude]\n1 failed")

    check = canaries.run_canaries(repo, _Adapter("1.1.0"), version="1.1.0")  # type: ignore[arg-type]

    assert check.status == "fail" and "manual-as-yolo" in check.detail


def test_a_missed_canary_reports_which_obligations_were_not_as_expected(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    line = (
        "harness_compat/conformance/canaries/test_canaries.py:93: "
        "AssertionError: {'mcp:call': 'fail'}"
    )
    seen = _pytest(monkeypatch, 1, f"{'.' * 800}\n{line}\nFAILED x::test\n1 failed")

    check = canaries.run_canaries(repo, _Adapter())  # type: ignore[arg-type]

    assert "{'mcp:call': 'fail'}" in check.detail
    assert "--tb=line" in seen[0]["argv"]


@pytest.mark.parametrize(
    ("returncode", "stdout", "needle"),
    [(0, "6 skipped in 2s", "no canary applied"), (4, "usage: pytest", "pytest exit 4")],
)
def test_no_applicable_canary_or_a_broken_run_is_no_verdict(
    repo: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, needle: str
) -> None:
    _pytest(monkeypatch, returncode, stdout)

    check = canaries.run_canaries(repo, _Adapter())  # type: ignore[arg-type]

    assert check.status == "error" and needle in check.detail


def test_a_binary_that_reports_another_release_is_not_canaried(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _pytest(monkeypatch, 0, "1 passed")

    check = canaries.run_canaries(repo, _Adapter("1.0.0"))  # type: ignore[arg-type]

    assert check.status == "error" and "reports 1.0.0" in check.detail and not seen


def test_a_failed_install_is_no_verdict(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _pytest(monkeypatch, 0, "1 passed")

    class Broken(_Adapter):
        def install(self, sb: Sandbox, version: str) -> Path:
            raise InstallFailed("npm exploded")

    check = canaries.run_canaries(repo, Broken())  # type: ignore[arg-type]

    assert check.status == "error" and "npm exploded" in check.detail and not seen


def test_the_canaries_run_in_a_disposable_copy_that_leaves_the_checkout_alone(
    repo: Path, monkeypatch: pytest.MonkeyPatch, git
) -> None:
    seen = _pytest(monkeypatch, 0, "2 passed in 9s")

    canaries.run_canaries(repo, _Adapter())  # type: ignore[arg-type]

    run = seen[0]
    assert run["cwd"] != repo and not run["cwd"].exists()
    assert ".git" not in run["files"] and "tracked.txt" in run["files"]
    assert run["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert run["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(run["cwd"])
    assert "no:cacheprovider" in run["argv"]
    assert git(repo, "status", "--porcelain") == ""


def test_a_release_the_policy_does_not_admit_is_admitted_alone_in_the_copy_only(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _pytest(monkeypatch, 0, "2 passed in 9s")

    canaries.run_canaries(repo, _Adapter("1.9.0"), version="1.9.0")  # type: ignore[arg-type]

    assert seen[0]["policy"] == VersionPolicy("1.9.0", "1.9.0")
    assert load_policy(repo, "claude") == VersionPolicy("1.0.0", "1.2.0")


def test_a_scenario_copying_the_copy_again_gets_the_same_tree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested: list[VersionPolicy] = []

    def fake(argv, *, cwd, env, timeout):
        with code_copy(cwd) as inner:
            nested.append(load_policy(inner, "claude"))
            assert not (inner / ".git").exists()
        return subprocess.CompletedProcess(argv, 0, "1 passed", "")

    monkeypatch.setattr(canaries, "run_group", fake)

    canaries.run_canaries(repo, _Adapter("1.9.0"), version="1.9.0")  # type: ignore[arg-type]

    assert nested == [VersionPolicy("1.9.0", "1.9.0")]
