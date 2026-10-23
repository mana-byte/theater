"""Release qualification policy: ranges, candidates, evidence reuse, promotion, artifacts."""

from __future__ import annotations

import json
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
    verdict_from_checks,
    version_key,
)
from scripts.harness_compat.evidence import (
    _proved,
    adapter_digest,
    cache_artifacts_dir,
    cache_root,
    reusable_evidence,
    write_evidence,
)
from scripts.harness_compat.publication import prepare_publication
from scripts.harness_compat.report import build_report
from scripts.harness_compat.runner import _qualify, classify_routes
from scripts.harness_compat.select import newest_in_range, plan_bump, select_candidates
from scripts.harness_compat.stale import stale_references, stale_warnings

pytestmark = pytest.mark.usefixtures("isolated_evidence_cache")


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
    assert select_candidates(adapter, gate, {"1.2.0"}, max_candidates=1) == ["1.3.0"]
    assert select_candidates(adapter, gate, {"1.4.0"}, version="1.4.0") == ["1.4.0"]
    with pytest.raises(ValueError, match="not a stable release"):
        select_candidates(adapter, gate, version="1.4.0-rc1")
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


@pytest.mark.parametrize(
    ("candidate_verdict", "issue_action"),
    [("green", "close"), ("red", "upsert")],
)
def test_range_pr_lists_green_results_and_keeps_red_issue(
    tmp_path: Path, candidate_verdict: str, issue_action: str
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
        "gate_kind": "range",
        "reused_results": [{**cached.to_evidence(), "run_url": "https://example.invalid/old"}],
        "results": [{**latest.to_evidence(), "run_url": "https://example.invalid/new"}],
        "watch": None,
        "watch_reused": False,
        "greens": ["1.6.0"],
        "inconclusive": False,
        "published": True,
        "run_url": "https://example.invalid/new",
    }
    report = prepare_publication(summary, tmp_path / "summary.json")
    assert report.pr_needed is True
    expected = ["- ✅ `1.6.0` — green · [run](https://example.invalid/old)"]
    if candidate_verdict == "green":
        expected.append("- ✅ `2.0.0` — green · [run](https://example.invalid/new)")
    assert [line for line in report.pr_body.splitlines() if line.startswith("- ✅")] == expected
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
    artifact_roots = (Path("candidates"), Path("fixtures"))
    expected_skips: ClassVar[dict[str, str]] = {}
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return ["1.2.0"]

    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet("range", floor="1.0.0", ceiling=(root / "gate.txt").read_text().strip())

    def install(self, sb: Sandbox, version: str) -> Path:
        return sb.bin_dir / version

    def version_line(self, version: str) -> str:
        return version

    def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
        major, minor, patch = version_key(greens[-1])
        (root / "gate.txt").write_text(f"{major}.{minor}.{patch + 1}\n")
        changed = [Path("gate.txt")]
        for version in greens:
            staged = root / f"candidates/{version}.json"
            if staged.exists():
                target = root / f"fixtures/{version}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(staged, target)
                changed.append(target.relative_to(root))
        return changed

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        staged = root / "candidates/1.2.0.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text('{"proved": true}\n')
        return Result(self.name, version, "green", (Check("proof", "pass"),))


class TwoReleaseAdapter(ArtifactAdapter):
    """Codex-shaped: qualify moves its bundle into fixtures/ itself, so capture sees that path."""

    def discover(self) -> list[str]:
        return ["1.2.0", "1.2.1", "1.3.0"]

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        staged = root / f"candidates/{version}.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text('{"proved": true}\n')
        self.apply_bump(root, [version])
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


def _artifact_repo(root: Path, gate: str, monkeypatch: pytest.MonkeyPatch) -> None:
    adapters = root / "scripts/harness_compat/adapters"
    adapters.mkdir(parents=True)
    (root / "scripts/harness_compat/contract.py").write_text("contract\n")
    (adapters / "vibe.py").write_text("adapter\n")
    (root / "gate.txt").write_text(gate)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "compat@example.invalid")
    _git(root, "config", "user.name", "Compat")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "source")
    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: [])


def test_green_candidate_artifact_is_composed_and_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _artifact_repo(tmp_path, "1.2.0\n", monkeypatch)

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
    pool = CandidateArtifacts(tmp_path / "collision-pool", [Path("same.txt")])
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
    assert CandidateArtifacts(tmp_path / "forbidden-pool", [Path("tests")]).capture(
        first, "1.2.0"
    ) == ["candidate wrote forbidden evidence path: tests/fixtures/harness_compat/vibe/1.2.0.json"]


def test_range_artifact_watch_and_deferral_keep_only_the_promoted_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _artifact_repo(tmp_path, "1.2.0\n", monkeypatch)
    calls: list[str] = []

    class WatchingArtifactAdapter(ArtifactAdapter):
        def discover(self) -> list[str]:
            return ["1.1.0", "1.2.0", "1.3.0", "1.4.0"]

        def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
            calls.append(version)
            if version == "1.2.0":
                return super().qualify(root, version, sb, binary)
            status = "fail" if version == "1.3.0" else "pass"
            return Result(
                self.name,
                version,
                "red" if status == "fail" else "green",
                (Check("proof", status),),
            )

    summary = runner.run_harness(WatchingArtifactAdapter(), tmp_path, publish=True)

    assert calls == ["1.2.0", "1.3.0", "1.1.0"]
    assert summary["candidates"] == ["1.2.0", "1.3.0", "1.4.0"]
    assert summary["deferred"] == ["1.4.0"]
    assert (summary["greens"], summary["published"]) == (["1.2.0"], True)
    assert summary["watch"]["version"] == "1.1.0"
    assert summary["watch"]["verdict"] == "green" and not summary["watch_reused"]
    assert [row["version"] for row in summary["issues"]] == ["1.3.0"]
    assert (tmp_path / "fixtures/1.2.0.json").is_file()
    assert not (tmp_path / "fixtures/1.3.0.json").exists()


def test_unpromoted_candidate_artifacts_stay_out_of_the_composed_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cached green above an unproved gap must not bring its bundle into the gate PR."""
    _artifact_repo(tmp_path, "1.2.0\n", monkeypatch)
    adapter = TwoReleaseAdapter()
    older, gap, newer = adapter.discover()
    first = runner.run_harness(adapter, tmp_path, version=newer, publish=True)
    assert (first["candidates"], first["greens"], first["published"]) == ([newer], [], False)
    assert (cache_artifacts_dir(tmp_path, "vibe", newer) / f"fixtures/{newer}.json").is_file()

    second = runner.run_harness(adapter, tmp_path, max_candidates=1, publish=True)

    assert (second["candidates"], second["greens"], second["published"]) == ([older], [older], True)
    assert second["reused"] == [newer] and gap not in second["reused"]
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert staged == [f"fixtures/{older}.json", "gate.txt"]
    assert not (tmp_path / f"fixtures/{newer}.json").exists()
    report = prepare_publication(second, tmp_path / "summary.json")
    assert [line for line in report.pr_body.splitlines() if line.startswith("- ✅")] == [
        f"- ✅ `{older}` — green · [run](local)",
        f"- ✅ `{newer}` — green · [run](local)",
    ]


class RetainedBundleAdapter(ArtifactAdapter):
    """Codex-shaped: qualify leaves an untracked candidate; the bump refreshes a TRACKED file."""

    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet("range", floor="1.0.0", ceiling=(root / "gate.txt").read_text().strip())

    def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
        (root / "gate.txt").write_text("1.2.1\n")
        changed = [Path("gate.txt")]
        staged = root / "candidates/1.2.0.json"
        if staged.exists():
            (root / "fixtures/latest.json").write_text(staged.read_text())
            staged.unlink()
            changed.append(Path("fixtures/latest.json"))
        return changed

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        staged = root / "candidates/1.2.0.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text('{"proved": true}\n')
        (root / "fixtures/latest.json").write_text(staged.read_text())  # lost: tracked edit
        return Result(self.name, version, "green", (Check("proof", "pass"),))


def test_refreshed_retained_bundle_is_staged_also_when_the_green_is_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tracked fixture is not captured; only its untracked candidate carries the refresh."""
    _artifact_repo(tmp_path, "1.2.0\n", monkeypatch)
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures/latest.json").write_text('{"proved": false}\n')
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "retained bundle")
    adapter = RetainedBundleAdapter()

    def staged() -> list[str]:
        return subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()

    assert runner.run_harness(adapter, tmp_path, publish=True)["published"] is True
    assert staged() == ["fixtures/latest.json", "gate.txt"]
    assert (tmp_path / "fixtures/latest.json").read_text() == '{"proved": true}\n'
    assert not (tmp_path / "candidates").exists()
    _git(tmp_path, "reset", "--hard", "HEAD")
    reused = runner.run_harness(adapter, tmp_path, publish=True)
    assert (reused["candidates"], reused["published"]) == ([], True)
    assert staged() == ["fixtures/latest.json", "gate.txt"]
    assert (tmp_path / "fixtures/latest.json").read_text() == '{"proved": true}\n'


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
        "tests/test_proof.py": "from tests.support import tables\n",
        "tests/test_table.py": "t",
        "tests/support/tables.py": "x = 1\n",
        "tests/conftest.py": (
            "from tests.support import fixture_helper\nfrom tests.support.shipped import pin\n"
        ),
        "tests/support/fixture_helper.py": "ok = True\n",
        "tests/support/shipped.py": "pin = 1\n",
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


@pytest.mark.parametrize(
    ("gate", "version", "admitted"),
    [
        (VerifiedSet("range", floor="2.24.0", ceiling="2.27.0"), "2.26.9", True),
        (VerifiedSet("range", floor="2.24.0", ceiling="2.27.0"), "2.27.0", False),
        (VerifiedSet("range", floor="2.24.0", ceiling="2.27.0"), "2.23.9", False),
        (
            VerifiedSet("range", floor="0.80.8", ceiling="0.86.0", known_bad=frozenset({"0.85.0"})),
            "0.85.0",
            False,
        ),
    ],
)
def test_gate_admits(gate: VerifiedSet, version: str, admitted: bool) -> None:
    assert gate.admits(version) is admitted


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        (("pass", "pass"), "green"),
        (("pass", "skip"), "green"),
        (("pass", "fail"), "red"),
        (("pass", "error"), "inconclusive"),
        (("skip",), "inconclusive"),
        ((), "inconclusive"),
    ],
)
def test_verdict_from_checks(statuses: tuple[str, ...], verdict: str) -> None:
    checks = [Check(f"t{index}", status) for index, status in enumerate(statuses)]  # type: ignore[arg-type]
    assert verdict_from_checks(checks) == verdict
