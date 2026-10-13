"""MVP policy and subprocess checks for harness qualification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from scripts.harness_compat import runner
from scripts.harness_compat.artifacts import CandidateArtifacts
from scripts.harness_compat.contract import (
    Check,
    RecordedRequest,
    Result,
    Sandbox,
    VerifiedSet,
)
from scripts.harness_compat.evidence import adapter_digest, reusable_evidence
from scripts.harness_compat.framework import run_pytest
from scripts.harness_compat.runner import classify_routes
from scripts.harness_compat.select import newest_in_range, plan_bump, select_candidates


class FakeAdapter:
    name = "vibe"
    expected_routes = frozenset({("POST", "/v1/chat/completions")})
    ignorable_routes = frozenset({("POST", "/telemetry")})

    def discover(self) -> list[str]:
        return ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def test_range_policy_evidence_reuse_routes_and_watch(tmp_path: Path) -> None:
    source = tmp_path / "scripts/harness_compat"
    adapters = source / "adapters"
    adapters.mkdir(parents=True)
    (source / "contract.py").write_text("contract\n")
    (adapters / "vibe.py").write_text("adapter\n")
    digest = adapter_digest(tmp_path, "vibe")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "compat@example.invalid")
    _git(tmp_path, "config", "user.name", "Compat")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "source")
    _git(tmp_path, "checkout", "-b", "rolling")
    evidence = tmp_path / "tests/fixtures/harness_compat/vibe/1.2.0.json"
    evidence.parent.mkdir(parents=True)
    document = {
        "harness": "vibe",
        "version": "1.2.0",
        "adapter_digest": digest,
        "verdict": "green",
        "checks": [],
    }
    evidence.write_text(json.dumps(document))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "evidence")
    _git(tmp_path, "checkout", "main")
    assert reusable_evidence(tmp_path, "vibe", digest, branch="rolling") == {"1.2.0": document}
    gate = VerifiedSet("range", floor="1.0.0", ceiling="1.2.0", known_bad=frozenset({"1.4.0"}))
    adapter = FakeAdapter()
    assert select_candidates(adapter, gate, {"1.2.0"}) == ["1.3.0", "1.5.0"]
    assert newest_in_range(adapter, gate) == "1.1.0"
    results = [
        Result("vibe", "1.2.0", "green"),
        Result("vibe", "1.3.0", "green"),
        Result("vibe", "1.5.0", "red"),
    ]
    assert plan_bump(gate, results) == ["1.2.0", "1.3.0"]
    route = RecordedRequest("POST", "/unknown", False, ("messages", "model"), True)
    classified = classify_routes(adapter, results[-1], (route,))
    assert classified.verdict == "tooling"
    assert "POST /unknown body keys=messages,model" in classified.checks[-1].detail
    assert classify_routes(adapter, results[-1], ()).verdict == "red"


class ArtifactAdapter:
    name = "vibe"
    probe = "unused:probe"
    stub_help = ""
    green_tests = ("proof",)
    consistency_tests: tuple[str, ...] = ()
    expected_skips: ClassVar[dict[str, str]] = {}
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return ["1.2.0"]

    def verified(self, root: Path) -> VerifiedSet:
        admitted = (
            frozenset({"1.2.0"}) if (root / "gate.txt").read_text() == "1.2.0\n" else frozenset()
        )
        return VerifiedSet("exact", versions=admitted)

    def install(self, sb: Sandbox, version: str) -> Path:
        return sb.bin_dir / version

    def version_line(self, version: str) -> str:
        return version

    def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
        (root / "gate.txt").write_text("1.2.0\n")
        changed = [Path("gate.txt")]
        staged = root / "candidates/1.2.0.json"
        if staged.exists():
            target = root / "fixtures/1.2.0.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(staged, target)
            changed.append(Path("fixtures/1.2.0.json"))
        return changed

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        staged = root / "candidates/1.2.0.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text('{"proved": true}\n')
        return Result(self.name, version, "green", (Check("proof", "pass"),))


def test_green_candidate_artifact_is_composed_and_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scripts/harness_compat"
    adapters = source / "adapters"
    adapters.mkdir(parents=True)
    (source / "contract.py").write_text("contract\n")
    (adapters / "vibe.py").write_text("adapter\n")
    (tmp_path / "gate.txt").write_text("none\n")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "compat@example.invalid")
    _git(tmp_path, "config", "user.name", "Compat")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "source")
    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: [])

    summary = runner.run_harness(ArtifactAdapter(), tmp_path, publish=True)

    assert summary["published"] is True
    assert (tmp_path / "fixtures/1.2.0.json").read_text() == '{"proved": true}\n'
    assert not (tmp_path / "candidates/1.2.0.json").exists()
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert staged == [
        "fixtures/1.2.0.json",
        "gate.txt",
        "tests/fixtures/harness_compat/vibe/1.2.0.json",
    ]
    _git(tmp_path, "checkout", "-b", "rolling")
    _git(tmp_path, "commit", "-m", "rolling evidence")
    _git(tmp_path, "checkout", "main")
    reused = runner.run_harness(
        ArtifactAdapter(), tmp_path, publish=True, evidence_branch="rolling"
    )
    assert reused["candidates"] == []
    assert reused["published"] is True
    assert (tmp_path / "fixtures/1.2.0.json").read_text() == '{"proved": true}\n'
    assert not (tmp_path / "candidates/1.2.0.json").exists()
    pool = CandidateArtifacts(tmp_path / "collision-pool")
    first, second = tmp_path / "collision-1", tmp_path / "collision-2"
    for tree in (first, second):
        tree.mkdir()
        _git(tree, "init")
        (tree / "same.txt").write_text("proof\n")
    assert pool.capture(first, "1.2.0") == []
    assert pool.capture(second, "1.3.0") == [
        "candidate artifact same.txt conflicts between 1.2.0 and 1.3.0"
    ]


def test_run_pytest_maps_skips_and_failures(tmp_path: Path) -> None:
    test = tmp_path / "test_sample.py"
    test.write_text(
        "import pytest\n"
        "def test_pass(): assert True\n"
        "def test_fail(): assert False\n"
        "def test_expected(): pytest.skip('expected')\n"
        "def test_unexpected(): pytest.skip('unexpected')\n"
    )
    nodes = tuple(
        f"test_sample.py::test_{name}" for name in ("pass", "fail", "expected", "unexpected")
    )
    checks = run_pytest(
        tmp_path,
        nodes,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        expected_skips={nodes[2]: "known"},
        timeout=30,
    )
    assert [check.status for check in checks] == ["pass", "fail", "skip", "error"]
    assert all(isinstance(check, Check) for check in checks)
