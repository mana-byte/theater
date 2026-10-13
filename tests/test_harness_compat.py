"""MVP policy and subprocess checks for harness qualification."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from scripts.harness_compat.contract import (
    Check,
    RecordedRequest,
    Result,
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
    evidence.write_text(json.dumps({"adapter_digest": digest, "verdict": "green"}))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "evidence")
    _git(tmp_path, "checkout", "main")
    assert reusable_evidence(tmp_path, "vibe", digest, branch="rolling") == {
        "1.2.0": {"adapter_digest": digest, "verdict": "green"}
    }
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
