"""MVP policy and subprocess checks for harness qualification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
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
from scripts.harness_compat.diagnostics import clip_diagnostic
from scripts.harness_compat.evidence import (
    _proved,
    adapter_digest,
    cache_artifacts_dir,
    cache_root,
    reusable_evidence,
    write_evidence,
)
from scripts.harness_compat.framework import _check_selector, run_pytest
from scripts.harness_compat.process import SANDBOX_ENV, new_token, owned_pids, run_group
from scripts.harness_compat.publication import prepare_publication
from scripts.harness_compat.report import _checks, build_report
from scripts.harness_compat.runner import _qualify, classify_routes
from scripts.harness_compat.sandbox import make_sandbox
from scripts.harness_compat.select import newest_in_range, plan_bump, select_candidates
from scripts.harness_compat.stale import stale_references, stale_warnings


class FakeAdapter:
    name = "vibe"
    expected_routes = frozenset({("POST", "/v1/chat/completions")})
    ignorable_routes = frozenset({("POST", "/telemetry")})

    def discover(self) -> list[str]:
        return ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"]


class ExactAdapter:
    def discover(self) -> list[str]:
        return [
            "0.1.0",
            "1.4.0",
            "1.5.0",
            "1.6.0",
            "1.7.0",
            "1.8.0",
            "1.9.0",
            "2.0.0",
        ]


@pytest.fixture(autouse=True)
def _isolated_evidence_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cache = tmp_path.parent / f"{tmp_path.name}-cache"
    monkeypatch.setenv("HARNESS_COMPAT_CACHE", str(cache))
    yield
    shutil.rmtree(cache, ignore_errors=True)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def test_range_policy_evidence_reuse_routes_and_watch(tmp_path: Path) -> None:
    source = tmp_path / "scripts/harness_compat"
    adapters = source / "adapters"
    adapters.mkdir(parents=True)
    (source / "contract.py").write_text("contract\n")
    (adapters / "vibe.py").write_text("adapter\n")
    digest = adapter_digest(tmp_path, "vibe")
    result = Result(
        "vibe",
        "1.2.0",
        "green",
        (Check("tests/proof.py::test_real", "pass"),),
        adapter_digest=digest,
    )
    evidence = write_evidence(tmp_path, result, "https://example.invalid/run/1")
    document = json.loads(evidence.read_text())
    assert evidence == cache_root(tmp_path) / "vibe/1.2.0.json"
    required = ("tests/proof.py::test_real",)
    reused = reusable_evidence(tmp_path, "vibe", digest, required=required)
    assert reused == {"1.2.0": document}
    assert reusable_evidence(tmp_path, "vibe", digest) == {}
    assert reusable_evidence(tmp_path, "vibe", "stale", required=required) == {}
    gate = VerifiedSet("range", floor="1.0.0", ceiling="1.2.0", known_bad=frozenset({"1.4.0"}))
    adapter = FakeAdapter()
    assert select_candidates(adapter, gate, {"1.2.0"}) == ["1.3.0", "1.5.0"]
    assert newest_in_range(adapter, gate) == "1.1.0"
    results = [
        Result("vibe", "1.2.0", "green"),
        Result("vibe", "1.3.0", "green"),
        Result("vibe", "1.5.0", "red"),
    ]
    assert plan_bump(gate, results, adapter.discover()) == ["1.2.0", "1.3.0"]
    route = RecordedRequest("POST", "/unknown", False, ("messages", "model"), True)
    classified = classify_routes(adapter, results[-1], (route,))
    assert classified.verdict == "tooling"
    assert "POST /unknown body keys=messages,model" in classified.checks[-1].detail
    assert classify_routes(adapter, results[-1], ()).verdict == "red"


def test_cached_artifacts_must_match_the_digest(tmp_path: Path) -> None:
    artifact = cache_artifacts_dir(tmp_path, "vibe", "1.2.0") / "generated.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("original")
    result = Result("vibe", "1.2.0", "green", (Check("proof", "pass"),), adapter_digest="d")
    write_evidence(tmp_path, result, "local", artifacts={"generated.json": "not-the-digest"})
    assert reusable_evidence(tmp_path, "vibe", "d", required=("proof",)) == {}


def test_exact_candidates_stay_above_oldest_admission() -> None:
    adapter = ExactAdapter()
    gate = VerifiedSet(
        "exact",
        versions=frozenset({"1.5.0", "1.7.0"}),
        known_bad=frozenset({"1.9.0"}),
    )
    assert select_candidates(adapter, gate, max_candidates=2) == ["2.0.0"]
    assert select_candidates(adapter, gate, {"2.0.0"}, max_candidates=2) == ["1.8.0"]
    assert select_candidates(adapter, gate, {"2.0.0"}, version="1.9.0") == ["1.9.0"]
    assert newest_in_range(adapter, gate) is None
    assert select_candidates(
        adapter, VerifiedSet("exact", versions=frozenset()), max_candidates=2
    ) == ["2.0.0"]


def test_exact_candidates_exhaust_new_releases_then_holes() -> None:
    adapter = ExactAdapter()
    gate = VerifiedSet(
        "exact",
        versions=frozenset({"1.5.0", "1.7.0"}),
        known_bad=frozenset({"1.9.0"}),
    )
    evidenced = {"1.8.0", "2.0.0"}
    assert select_candidates(adapter, gate, evidenced) == ["1.6.0"]
    evidenced.add("1.6.0")
    assert select_candidates(adapter, gate, evidenced) == []
    results = [
        Result("codex", "1.4.0", "green"),
        Result("codex", "1.6.0", "green"),
        Result("codex", "2.0.0", "green"),
    ]
    assert plan_bump(gate, results, adapter.discover()) == ["2.0.0"]
    results[-1] = Result("codex", "2.0.0", "red")
    assert plan_bump(gate, results, adapter.discover()) == ["1.6.0"]
    assert plan_bump(gate, [results[-1]], adapter.discover()) == []


@pytest.mark.parametrize(
    ("candidate_verdict", "promoted", "issue_action"),
    [("green", "2.0.0", "close"), ("red", "1.6.0", "upsert")],
)
def test_exact_pr_lists_only_promoted_green_and_keeps_red_issue(
    tmp_path: Path, candidate_verdict: str, promoted: str, issue_action: str
) -> None:
    cached = Result("codex", "1.6.0", "green", (Check("proof", "pass"),))
    latest = Result(
        "codex",
        "2.0.0",
        candidate_verdict,
        (Check("proof", "fail" if candidate_verdict == "red" else "pass"),),
    )
    summary = {
        "harness": "codex",
        "gate_kind": "exact",
        "reused_results": [{**cached.to_evidence(), "run_url": "https://example.invalid/old"}],
        "results": [{**latest.to_evidence(), "run_url": "https://example.invalid/new"}],
        "watch": None,
        "watch_reused": False,
        "greens": [promoted],
        "inconclusive": False,
        "published": True,
        "run_url": "https://example.invalid/new",
    }
    report = prepare_publication(summary, tmp_path / "summary.json")
    assert report.pr_needed is True
    expected_run = "new" if promoted == "2.0.0" else "old"
    assert [line for line in report.pr_body.splitlines() if line.startswith("- ✅")] == [
        f"- ✅ `{promoted}` — green · [run](https://example.invalid/{expected_run})"
    ]
    assert report.issue_action == issue_action
    assert ("| 2.0.0 | red |" in report.issue_body) is (candidate_verdict == "red")


def test_range_promotion_never_skips_an_unproved_release() -> None:
    gate = VerifiedSet("range", floor="1.0.0", ceiling="1.1.0")
    releases = ["1.0.0", "1.1.0", "1.1.1", "1.2.0"]
    assert plan_bump(gate, [Result("x", "1.2.0", "green")], releases) == []  # forced above a gap
    capped = [Result("x", "1.1.0", "green"), Result("x", "1.2.0", "green")]  # 1.1.1 over the cap
    assert plan_bump(gate, capped, releases) == ["1.1.0"]
    full = [*capped, Result("x", "1.1.1", "green")]
    assert plan_bump(gate, full, releases) == ["1.1.0", "1.1.1", "1.2.0"]


class LimitAdapter:
    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet("range", floor="0.1.0", ceiling=(root / "gate.txt").read_text().strip())


def _limit_repo(root: Path, old_ceiling: str, old_inclusive: str) -> None:
    (root / "gate.txt").write_text(f"{old_ceiling}\n")
    (root / "docs.txt").write_text(f"<= {old_inclusive}\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "compat@example.invalid")
    _git(root, "config", "user.name", "Compat")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "old limit")


@pytest.mark.parametrize(
    ("old_ceiling", "old_inclusive", "new_ceiling", "new_inclusive"),
    [
        ("0.99.2", "0.99.1", "0.99.3", "0.99.2"),
        ("1.18.35", "1.18.34", "1.18.36", "1.18.35"),
    ],
)
def test_old_limit_scan_allows_new_inclusive_bound(
    tmp_path: Path,
    old_ceiling: str,
    old_inclusive: str,
    new_ceiling: str,
    new_inclusive: str,
) -> None:
    _limit_repo(tmp_path, old_ceiling, old_inclusive)
    (tmp_path / "gate.txt").write_text(f"{new_ceiling}\n")
    (tmp_path / "docs.txt").write_text(f"< {new_ceiling} and <= {new_inclusive}\n")
    adapter = LimitAdapter()
    assert stale_references(adapter, tmp_path) == ([], [])


def test_old_limit_scan_finds_only_whole_stale_versions(tmp_path: Path) -> None:
    _limit_repo(tmp_path, "0.99.2", "0.99.1")
    (tmp_path / "gate.txt").write_text("0.99.3\n")
    source = tmp_path / "plugin.py"
    source.write_text("limit = '<= 0.99.2'; unrelated = '10.99.1, 0.99.10, 0.99.1.5'\n")
    adapter = LimitAdapter()
    assert stale_references(adapter, tmp_path) == ([], [])
    source.write_text(source.read_text() + "limit = '<= 0.99.1'\n")
    assert stale_references(adapter, tmp_path) == (
        ["old limit 0.99.1 remains in plugin.py:2"],
        [],
    )


def test_old_limit_scan_handles_sentence_final_periods(tmp_path: Path) -> None:
    _limit_repo(tmp_path, "0.99.2", "0.99.1")
    (tmp_path / "gate.txt").write_text("0.99.3\n")
    (tmp_path / "plugin.py").write_text(
        "qualified releases are 0.85.1-0.99.1.\n"
        "requires >=0.85.1,<=0.99.1.\n"
        "historical neighbors 10.99.1, 0.99.10, 0.99.1.5.\n"
    )
    assert stale_references(LimitAdapter(), tmp_path) == (
        ["old limit 0.99.1 remains in plugin.py:1", "old limit 0.99.1 remains in plugin.py:2"],
        [],
    )


def test_pi_historical_proofs_are_soft_but_bounds_are_hard(tmp_path: Path) -> None:
    _limit_repo(tmp_path, "0.99.2", "0.99.1")
    (tmp_path / "gate.txt").write_text("0.99.3\n")
    docs = tmp_path / "docs.txt"
    docs.write_text("requires <=0.99.1\n")
    readme = tmp_path / "README.md"
    readme.write_text("qualified <=0.99.1\n")
    source = tmp_path / "plugin.py"
    source.write_text("# 0.99.1 was proven on stock Pi\n")
    adapter = LimitAdapter()
    assert stale_references(adapter, tmp_path) == (
        [],
        ["plugin.py:1 (0.99.1)"],
    )
    assert stale_warnings(adapter, tmp_path, ["0.99.2"]) == ["plugin.py:1 (0.99.1)"]
    source.write_text(
        "# 0.99.1 was proven on stock Pi\n"
        "in_qualified = (0, 85, 1) <= parsed <= (0, 99, 1)\n"
        'range_text = "<= 0.99.1"\n'
    )
    assert stale_references(adapter, tmp_path)[0] == [
        "old limit 0.99.1 remains in plugin.py:2",
        "old limit 0.99.1 remains in plugin.py:3",
    ]


def test_opencode_historical_header_is_soft_but_range_string_and_tuple_are_hard(
    tmp_path: Path,
) -> None:
    _limit_repo(tmp_path, "1.18.35", "1.18.34")
    (tmp_path / "gate.txt").write_text("1.18.36\n")
    (tmp_path / "docs.txt").write_text("Qualified: 1.17.15–1.18.34\n")
    source = tmp_path / "runtime_plan.py"
    source.write_text(
        "# 1.17.15-1.18.34 qualified by byte-stable Theater-consumed surfaces\n"
        "# model-free live probes on stock 1.17.15/1.18.34\n"
        "# the paid suite is pinned to stock 1.18.34 (previously 1.18.29)\n"
    )
    adapter = LimitAdapter()
    assert stale_references(adapter, tmp_path) == (
        [],
        [
            "runtime_plan.py:1 (1.18.34)",
            "runtime_plan.py:2 (1.18.34)",
            "runtime_plan.py:3 (1.18.34)",
        ],
    )
    source.write_text(
        source.read_text()
        + 'reason = "(qualified releases are 1.17.15-1.18.34)"\n'
        + "OPENCODE_TUI_MAX_VERSION = (1, 18, 35)\n"
    )
    assert stale_references(adapter, tmp_path)[0] == [
        "old limit 1.18.34 remains in runtime_plan.py:4",
        "old limit 1.18.35 remains in runtime_plan.py:5",
    ]


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


class EvidenceOnlyAdapter:
    name = "claude"
    probe = "unavailable:probe"
    stub_help = ""
    green_tests = ("proof",)
    consistency_tests: tuple[str, ...] = ()
    expected_skips: ClassVar[dict[str, str]] = {}
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return ["3.0.0"]

    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet("range", floor="2.0.0", ceiling="3.0.0")

    def install(self, sb: Sandbox, version: str) -> Path:
        return sb.bin_dir / version

    def version_line(self, version: str) -> str:
        return version

    def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
        return []

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        return Result(self.name, version, "green", (Check("proof", "pass"),))


def _evidence_only_repo(root: Path) -> None:
    source = root / "scripts/harness_compat"
    adapters = source / "adapters"
    adapters.mkdir(parents=True)
    (source / "contract.py").write_text("contract\n")
    (adapters / "claude.py").write_text("adapter\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "compat@example.invalid")
    _git(root, "config", "user.name", "Compat")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "source")


def test_no_change_green_is_cached_without_a_pr(tmp_path: Path) -> None:
    _evidence_only_repo(tmp_path)
    summary = runner.run_harness(
        EvidenceOnlyAdapter(), tmp_path, publish=True, run_url="https://example.invalid/run/1"
    )
    assert summary["published"] is False
    assert summary["greens"] == ["3.0.0"]
    document = json.loads((cache_root(tmp_path) / "claude/3.0.0.json").read_text())
    result = Result(
        "claude",
        "3.0.0",
        "green",
        (Check("proof", "pass"),),
        adapter_digest=summary["adapter_digest"],
    )
    assert document == {
        **result.to_evidence(),
        "run_url": "https://example.invalid/run/1",
        "artifacts": {},
    }
    assert (
        subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )
    report = prepare_publication(summary, tmp_path / "summary.json")
    assert report.pr_needed is False


def test_forced_in_range_candidate_reuses_its_watch_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _evidence_only_repo(tmp_path)
    calls: list[str] = []

    class InRangeAdapter(EvidenceOnlyAdapter):
        def verified(self, root: Path) -> VerifiedSet:
            return VerifiedSet("range", floor="2.0.0", ceiling="4.0.0")

        def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
            return []

        def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
            calls.append(version)
            return super().qualify(root, version, sb, binary)

    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: [])
    summary = runner.run_harness(InRangeAdapter(), tmp_path, version="3.0.0")
    assert calls == ["3.0.0"]
    assert summary["watch"] == summary["results"][0]
    assert summary["watch_reused"] is True
    assert summary["watch"]["verdict"] == "green"
    route = {"method": "POST", "path": "/v1/chat/completions"}
    summary["results"][0]["routes"] = [route]
    summary["watch"]["routes"] = [route]
    report = prepare_publication(summary, tmp_path / "summary.json")
    assert report.pr_needed is False
    assert "Routes:" not in report.pr_body


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
    ]
    cached = json.loads((cache_root(tmp_path) / "vibe/1.2.0.json").read_text())
    assert set(cached["artifacts"]) == {"candidates/1.2.0.json"}
    assert (cache_artifacts_dir(tmp_path, "vibe", "1.2.0") / "candidates/1.2.0.json").is_file()
    _git(tmp_path, "checkout", "-b", "rolling")
    _git(tmp_path, "commit", "-m", "rolling gate")
    _git(tmp_path, "checkout", "main")
    reused = runner.run_harness(ArtifactAdapter(), tmp_path, publish=True)
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
    forbidden = first / "tests/fixtures/harness_compat/vibe/1.2.0.json"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("legacy evidence")
    assert CandidateArtifacts(tmp_path / "forbidden-pool").capture(first, "1.2.0") == [
        "candidate wrote forbidden evidence path: tests/fixtures/harness_compat/vibe/1.2.0.json"
    ]


def test_pr_body_is_one_line_per_release_with_bounded_warnings() -> None:
    rows = (
        Result("pi", "0.99.2", "green", (Check("probe", "pass"), Check("tui", "skip"))),
        Result("pi", "0.99.3", "green", (Check("probe", "pass"), Check("tui", "skip"))),
    )
    report = build_report(
        "pi",
        rows,
        run_url="https://example.invalid/current",
        release_runs={"0.99.2": "https://example.invalid/prior"},
        stale_warnings=(f"plugin.py:{n} (0.99.1)" for n in range(9)),
    )
    lines = report.pr_body.splitlines()
    assert [line for line in lines if line.startswith("- ✅")] == [
        "- ✅ `0.99.2` — green · [run](https://example.invalid/prior)",
        "- ✅ `0.99.3` — green · [run](https://example.invalid/current)",
    ]
    assert report.pr_body.count("Declared skips:") == 1
    assert "| Checks |" not in report.pr_body
    assert (
        len([line for line in lines if line.startswith("- plugin.py:") or line.startswith("- …")])
        == 5
    )


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


def test_failed_pytest_diagnostics_and_report_rows_are_bounded(tmp_path: Path) -> None:
    (tmp_path / "test_diagnostics.py").write_text(
        "import pytest\n"
        "@pytest.fixture\n"
        "def broken(): raise RuntimeError('setup crash')\n"
        "def test_fail():\n"
        "    print('captured output')\n"
        "    assert False, '\\x1b[31m' + 'X' * 1200 + '\\x1b[0m'\n"
        "def test_setup(broken): pass\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    failed, errored = run_pytest(
        tmp_path,
        ("test_diagnostics.py", "test_diagnostics.py::test_setup"),
        env=env,
        expected_skips={},
        timeout=30,
    )
    assert failed.status == "fail"
    assert failed.detail.startswith("1 failed, 1 errored; test_diagnostics.py::test_fail:")
    assert len(failed.detail) <= 600
    assert "[31m" not in failed.detail and "#x1B" not in failed.detail
    assert errored.status == "error"
    assert "test_diagnostics.py::test_setup:" in errored.detail
    assert "RuntimeError: setup crash" in errored.detail
    captured = ET.Element("testcase")
    ET.SubElement(captured, "failure", message="")
    ET.SubElement(captured, "system-out").text = "ignored " * 200 + "capture tail"
    fallback = _check_selector(
        "test_diagnostics.py::test_capture", {"test_diagnostics.py::test_capture": captured}, {}
    )
    assert fallback.status == "fail"
    assert "test_diagnostics.py::test_capture" in fallback.detail
    assert "capture tail" in fallback.detail

    rows = (
        Result("pi", "0.99.2", "red", (failed,)),
        Result("pi", "0.99.3", "tooling", (Check("mock route", "fail", "Y" * 4000),)),
    )
    report = build_report("pi", rows, run_url="local")
    issue_rows = [line for line in report.issue_body.splitlines() if line.startswith("| 0.99.")]
    assert len(issue_rows) == 2
    assert all(len(line) <= 1700 for line in issue_rows)
    assert "test_diagnostics.py::test_fail" in issue_rows[0]
    assert "[31m" not in report.issue_body
    inconclusive = Result("pi", "0.99.4", "inconclusive", (Check("infra", "error", "Z" * 4000),))
    assert len(_checks(inconclusive)) <= 1600


def test_run_pytest_aggregates_file_directory_class_and_parameter_selectors(tmp_path: Path) -> None:
    proofs = tmp_path / "proofs"
    proofs.mkdir()
    (proofs / "test_selectors.py").write_text(
        "import pytest\n"
        "def test_top(): assert True\n"
        "class TestBundleIndexAndAllowlist:\n"
        "    def test_first(self): assert True\n"
        "    def test_second(self): assert True\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_param(value): assert value > 0\n"
    )
    (tmp_path / "test_skip.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_skip(value): pytest.skip('unavailable')\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    file = "proofs/test_selectors.py"
    selectors = (
        "proofs",
        file,
        f"{file}::TestBundleIndexAndAllowlist",
        f"{file}::test_param",
        f"{file}::test_param[1]",
    )
    checks = run_pytest(tmp_path, selectors, env=env, expected_skips={}, timeout=30)
    assert [check.status for check in checks] == ["pass"] * len(selectors)
    assert [check.detail for check in checks] == [
        "5 passed",
        "5 passed",
        "2 passed",
        "2 passed",
        "1 passed",
    ]
    unknown = run_pytest(
        tmp_path, (f"{file}::test_missing",), env=env, expected_skips={}, timeout=30
    )
    assert unknown[0].status == "error"
    unexpected = run_pytest(tmp_path, ("test_skip.py",), env=env, expected_skips={}, timeout=30)
    assert unexpected[0].status == "error"
    assert "2 skipped" in unexpected[0].detail
    expected = run_pytest(
        tmp_path,
        ("test_skip.py::test_skip",),
        env=env,
        expected_skips={"test_skip.py::test_skip": "known"},
        timeout=30,
    )
    assert expected[0].status == "skip"
    assert expected[0].detail == "2 skipped"


def test_sandbox_keeps_unix_socket_paths_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_sandbox("codex") as sb:
        if Path("/tmp").is_dir():
            assert sb.root.is_relative_to(Path("/tmp").resolve())
        assert sb.root == sb.root.resolve()
        assert sb.env["TMPDIR"] == str(sb.root / "t")
        socket = (
            sb.evidence_dir
            / "0.162.100"
            / sb.home.name
            / ".codex/sockets"
            / f"app-server-{'0' * 32}.sock"
        )
        assert len(os.fsencode(socket)) < 100
        assert len(os.fsencode(sb.tmux_socket)) < 100
        for env in (sb.env, sb.offline_env):
            assert "ANTHROPIC_API_KEY" not in env
            assert "OPENAI_API_KEY" not in env

    long_root = tmp_path / ("x" * 120)

    def long_temp(*args: object, **kwargs: object) -> str:
        long_root.mkdir()
        return str(long_root)

    monkeypatch.setattr("scripts.harness_compat.sandbox.tempfile.mkdtemp", long_temp)
    with (
        pytest.raises(RuntimeError, match="tmux AF_UNIX socket path is too long"),
        make_sandbox("codex"),
    ):
        pass
    assert not long_root.exists()


def test_clip_keeps_the_named_cause_ahead_of_a_long_stack_tail() -> None:
    stack = " at frame (file.js:1:1)" * 40
    text = (
        "AssertionError: stderr: import { A } from 'pkg'; ^^^ SyntaxError: The requested "
        f"module 'pkg' does not provide an export named 'A' at ModuleJob.run{stack}"
    )
    clipped = clip_diagnostic(text, 300, tail=True)
    assert len(clipped) <= 300
    assert clipped.startswith("SyntaxError: The requested module 'pkg' does not provide")
    assert clipped.endswith("(file.js:1:1)")
    assert clip_diagnostic("plain failure " * 80, 100, tail=True).startswith("…")


def test_adapter_tooling_verdict_survives_missing_green_checks(tmp_path: Path) -> None:
    class ConsistencyFails(FakeAdapter):
        green_tests = ("tests/proof.py",)
        expected_skips: ClassVar[dict[str, str]] = {}

        def install(self, sb: Sandbox, version: str) -> Path:
            return tmp_path / "binary"

        def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
            return Result("vibe", version, "tooling", (Check("tests/unit.py", "fail", "table"),))

    result, _ = _qualify(ConsistencyFails(), tmp_path, "1.0.0", bump=False)  # type: ignore[arg-type]
    assert result.verdict == "tooling"
    assert result.checks[0].detail == "table"


def test_evidence_reuse_requires_every_declared_proof_to_have_passed() -> None:
    proof = ("tests/p.py::a", "tests/p.py::b[x]")

    def document(*checks: tuple[str, str]) -> dict[str, object]:
        return {"checks": [{"name": name, "status": status} for name, status in checks]}

    good = document(("tests/p.py::a", "pass"), ("tests/p.py::b[x]", "pass"), ("extra", "pass"))
    assert _proved(good, proof, ())
    assert not _proved(document(), proof, ())  # empty evidence proves nothing
    assert not _proved(document(("tests/p.py::a", "pass")), proof, ())  # a proof is missing
    failing = document(("tests/p.py::a", "pass"), ("tests/p.py::b[x]", "pass"), ("c", "fail"))
    assert not _proved(failing, proof, ())
    skipped = document(("tests/p.py::a", "pass"), ("tests/p.py::b[x]", "skip"))
    assert not _proved(skipped, proof, ())  # an undeclared skip is not a proof
    assert _proved(skipped, proof, {"tests/p.py::b[x]"})  # a declared skip is allowed
    twice = document(
        ("tests/p.py::a", "pass"), ("tests/p.py::a", "pass"), ("tests/p.py::b[x]", "pass")
    )
    assert not _proved(twice, proof, ())
    assert not _proved(good, (), ())  # an adapter without proofs cannot vouch for anything


def test_adapter_digest_covers_every_input_the_proofs_run_through(tmp_path: Path) -> None:
    class Proof:
        name = "vibe"
        green_tests = ("tests/test_proof.py::test_real",)
        consistency_tests = ("tests/test_table.py",)

    files = {
        "scripts/harness_compat/contract.py": "c",
        "scripts/harness_compat/artifacts.py": "r",
        "scripts/harness_compat/evidence.py": "e",
        "scripts/harness_compat/mock.py": "m",
        "scripts/harness_compat/adapters/vibe.py": "a",
        "scripts/harness_compat/adapters/vibe_store.py": "h",
        "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["tests"]\n',
        "tests/test_proof.py": "from tests.rig import tables\n",
        "tests/test_table.py": "t",
        "tests/rig/tables.py": "x = 1\n",
        "tests/conftest.py": "from tests.rig import fixture_helper\nfrom shipped import pin\n",
        "tests/rig/fixture_helper.py": "ok = True\n",
        "tests/shipped.py": "pin = 1\n",
        "tests/test_other.py": "o",
        "tests/native/helper.py": "n",
        "tests/fixtures/vibe_store/doc.json": "f",
        "tests/fixtures/harness_compat/vibe/1.0.0.json": "e",
        "theater/harness/builtin/plugins/vibe/unified_store.py": "u",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    base = adapter_digest(tmp_path, "vibe", Proof())  # type: ignore[arg-type]
    for name, original in files.items():
        if name == "pyproject.toml":  # it only tells the closure where bare imports resolve
            continue
        (tmp_path / name).write_text("changed")
        changed = adapter_digest(tmp_path, "vibe", Proof()) != base  # type: ignore[arg-type]
        # evidence files are outputs and an unimported test is no input
        outputs = name.startswith("tests/fixtures/harness_compat") or name == "tests/test_other.py"
        assert changed is (not outputs), name
        (tmp_path / name).write_text(original)
    assert adapter_digest(tmp_path, "vibe", Proof()) == base  # type: ignore[arg-type]


def test_early_conclusive_adapter_red_is_not_demoted_to_inconclusive(tmp_path: Path) -> None:
    class StaticFails(FakeAdapter):
        green_tests = ("tests/proof.py",)
        expected_skips: ClassVar[dict[str, str]] = {}

        def install(self, sb: Sandbox, version: str) -> Path:
            return tmp_path / "binary"

        def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
            check = Check("vibe_store_format", "fail", "upstream 9 > ours 8")
            return Result("vibe", version, "red", (check,))

    result, _ = _qualify(StaticFails(), tmp_path, "2.27.0", bump=False)  # type: ignore[arg-type]
    assert result.verdict == "red"
    assert result.checks[0].detail == "upstream 9 > ours 8"


def _gone(pid: int) -> bool:
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    os.kill(pid, 9)
    return False


def test_run_pytest_timeout_reaps_detached_children_it_started(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    (tmp_path / "test_hang.py").write_text(
        "import os, subprocess, time\n\n\n"
        "def test_hang():\n"
        "    child = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
        "    open(os.environ['CHILD_PIDFILE'], 'w').write(str(child.pid))\n"
        "    time.sleep(60)\n"
    )
    env = {**os.environ, "CHILD_PIDFILE": str(pidfile)}
    (check,) = run_pytest(tmp_path, ["test_hang.py"], env=env, expected_skips={}, timeout=3)
    assert check.status == "error"
    assert _gone(int(pidfile.read_text())), "the sleeping child outlived the timed-out pytest run"


def test_run_group_reaps_a_detached_child_after_the_leader_exited(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = (
        "import subprocess, sys\n"
        "child = subprocess.Popen(['sleep', '60'], start_new_session=True)\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
    )
    argv = [sys.executable, "-c", script, str(pidfile)]
    assert run_group(argv, cwd=tmp_path, env=os.environ, timeout=30).returncode == 0
    assert _gone(int(pidfile.read_text())), "the detached child outlived its finished leader"


def test_ownership_needs_an_exact_environment_entry() -> None:
    token = new_token()
    entry = f"{SANDBOX_ENV}={token}"
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    clean = {"PATH": os.environ["PATH"]}
    owned = subprocess.Popen(sleeper, env={**clean, SANDBOX_ENV: token})
    argument = subprocess.Popen([*sleeper, entry], env=clean)
    embedded = subprocess.Popen(sleeper, env={**clean, "NOTE": f"x {entry}"})
    try:
        found = owned_pids(SANDBOX_ENV, token)
        assert owned.pid in found
        assert argument.pid not in found and embedded.pid not in found
    finally:
        for process in (owned, argument, embedded):
            process.kill()
            process.wait()


def test_sandbox_removes_read_only_trees() -> None:
    with make_sandbox("vibe") as sb:
        locked = sb.config_dir / "plugins" / "pkg"
        locked.mkdir(parents=True)
        (locked / "file.txt").write_text("x")
        (locked / "file.txt").chmod(0o400)
        locked.chmod(0o500)
        hidden = sb.config_dir / "hidden"
        hidden.mkdir()
        (hidden / "file.txt").write_text("x")
        hidden.chmod(0)
        root = sb.root
    assert not root.exists()


def _tmux_alive(socket: Path) -> bool:
    done = subprocess.run(["tmux", "-S", str(socket), "ls"], capture_output=True, check=False)
    return done.returncode == 0


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_sandbox_teardown_stops_what_it_owns_and_nothing_else() -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    with make_sandbox("vibe") as sb:
        sockets = [sb.tmux_socket, sb.root / "second.sock"]
        for socket in sockets:
            subprocess.run(
                ["tmux", "-S", str(socket), "new-session", "-d", "sleep 60"],
                capture_output=True,
                check=True,
            )
            assert _tmux_alive(socket)
        owned = subprocess.Popen(sleeper, env=sb.env, start_new_session=True)
        bystander = subprocess.Popen([*sleeper, str(sb.root)], start_new_session=True)
    try:
        assert not any(_tmux_alive(socket) for socket in sockets)
        assert owned.wait(timeout=10) != 0  # detached, but it carries the sandbox token
        assert bystander.poll() is None  # naming the sandbox path is not ownership
    finally:
        bystander.kill()
        bystander.wait()
