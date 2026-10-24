"""The canaries command: the newest admitted release, and no silent pass."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.harness_compat import canaries
from scripts.harness_compat.contract import Sandbox, VerifiedSet


class Adapter:
    name = "acme"
    installed: list[str]

    def __init__(self, releases: tuple[str, ...] = ("1.0.0", "1.1.0", "2.0.0")) -> None:
        self.releases = releases
        self.installed = []

    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet("range", floor="1.0.0", ceiling="2.0.0")

    def discover(self) -> list[str]:
        return list(self.releases)

    def install(self, sb: Sandbox, version: str) -> Path:
        self.installed.append(version)
        return sb.bin_dir / "acme"


def _pytest(monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str) -> list[dict]:
    seen: list[dict] = []

    def fake(argv, *, cwd, env, timeout):
        seen.append({"argv": argv, "env": env})
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(canaries, "run_group", fake)
    return seen


def test_the_newest_admitted_release_is_installed_and_every_caught_canary_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _pytest(monkeypatch, 0, "4 passed, 2 skipped in 61s")
    adapter = Adapter()

    check = canaries.run_canaries(tmp_path, adapter)  # type: ignore[arg-type]

    assert adapter.installed == ["1.1.0"]
    assert (check.status, check.detail) == ("pass", "acme 1.1.0: 4 passed, 2 skipped")
    env = seen[0]["env"]
    assert env[canaries.ENV_ENABLE] == "1" and env[canaries.ENV_HARNESS] == "acme"
    assert env[canaries.ENV_BINARY].endswith("acme")
    assert seen[0]["argv"][-3:] == ["-k", "acme", canaries.CANARIES_TARGET]


def test_a_canary_the_suite_missed_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pytest(
        monkeypatch, 1, "FAILED test_canaries.py::test[manual-as-yolo-acme]\n1 failed, 3 passed"
    )

    check = canaries.run_canaries(tmp_path, Adapter(), version="1.0.0")  # type: ignore[arg-type]

    assert check.status == "fail"
    assert "manual-as-yolo" in check.detail


@pytest.mark.parametrize(
    ("returncode", "stdout", "needle"),
    [
        (0, "6 skipped in 2s", "no canary applied"),
        (4, "usage: pytest", "pytest exit 4"),
    ],
)
def test_no_applicable_canary_or_a_broken_run_is_no_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, needle: str
) -> None:
    _pytest(monkeypatch, returncode, stdout)

    check = canaries.run_canaries(tmp_path, Adapter())  # type: ignore[arg-type]

    assert check.status == "error" and needle in check.detail


def test_a_harness_with_nothing_admitted_has_nothing_to_install(tmp_path: Path) -> None:
    adapter = Adapter(releases=("3.0.0",))

    check = canaries.run_canaries(tmp_path, adapter)  # type: ignore[arg-type]

    assert check.status == "error" and adapter.installed == []
