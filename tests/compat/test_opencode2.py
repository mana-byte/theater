"""The OpenCode 2.x harness-compat arm: gate, bump, discovery and install."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.harness_compat import contract
from scripts.harness_compat.adapters import (
    opencode2,
    opencode2_bump,
    opencode_bump,
    opencode_sandbox,
)
from scripts.harness_compat.sandbox import make_sandbox
from tests.support.paths import REPO_ROOT


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    destination = tmp_path / opencode2_bump.RUNTIME_PLAN
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes((REPO_ROOT / opencode2_bump.RUNTIME_PLAN).read_bytes())
    return tmp_path


def test_the_gate_is_an_exclusive_range_that_may_end_in_dot_zero(tree: Path) -> None:
    floor, ceiling = opencode2_bump.read_gate(tree)
    gate = opencode2.ADAPTER.verified(tree)
    assert (gate.kind, gate.floor, gate.ceiling) == ("range", floor, ceiling)
    assert gate.admits(floor) and not gate.admits(ceiling)
    assert gate.evidenced == frozenset()


def test_the_bump_moves_only_the_2x_ceiling_and_not_the_1x_one(tree: Path) -> None:
    before = (tree / opencode2_bump.RUNTIME_PLAN).read_text(encoding="utf-8").splitlines()
    floor, ceiling = opencode2_bump.read_gate(tree)
    major, minor, patch = contract.version_key(ceiling)
    green = f"{major}.{minor}.{patch + 4}"
    assert opencode2.ADAPTER.apply_bump(tree, [ceiling, green]) == [opencode2_bump.RUNTIME_PLAN]
    assert opencode2_bump.read_gate(tree) == (floor, f"{major}.{minor}.{patch + 5}")
    assert opencode2.ADAPTER.apply_bump(tree, [ceiling, green]) == []  # idempotent
    after = (tree / opencode2_bump.RUNTIME_PLAN).read_text(encoding="utf-8").splitlines()
    assert [line for line in after if line not in before] == [
        f"OPENCODE_SERVER_V2_MAX_VERSION = ({major}, {minor}, {patch + 5})"
    ]
    assert opencode_bump.read_gate(tree) == opencode_bump.read_gate(REPO_ROOT)


def test_the_bump_refuses_other_majors_and_a_gap_below_the_ceiling(tree: Path) -> None:
    with pytest.raises(ValueError, match=r"only admits 2\.x"):
        opencode2.ADAPTER.apply_bump(tree, ["1.19.3"])
    with pytest.raises(ValueError, match="ascending"):
        opencode2.ADAPTER.apply_bump(tree, ["2.9.0", "2.8.0"])
    with pytest.raises(ValueError, match="current ceiling"):
        opencode2.ADAPTER.apply_bump(tree, ["2.0.0"])


def test_discovery_reads_the_2x_package_only(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def versions(package: str) -> list[str]:
        seen.append(package)
        return ["1.19.3", "2.0.20", "2.1.0", "3.0.0"]

    monkeypatch.setattr(opencode2.github, "npm_versions", versions)
    assert opencode2.ADAPTER.discover() == ["2.0.20", "2.1.0"]
    assert seen == ["@opencode/cli"]
    assert opencode2.ADAPTER.version_line("2.0.20") == "opencode v2.0.20"


def test_install_failure_is_inconclusive_and_names_the_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError("npm unavailable")

    monkeypatch.setattr(opencode_sandbox, "run_group", broken)
    with (
        make_sandbox("opencode2", root=tmp_path) as sb,
        pytest.raises(contract.InstallFailed, match=r"@opencode/cli@2\.0\.20 did not run"),
    ):
        opencode2.ADAPTER.install(sb, "2.0.20")
