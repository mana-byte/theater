"""Bounded retries, the publication truth table, and what a run shows on GitHub."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from scripts.harness_compat import runner
from scripts.harness_compat.artifacts import stage_composed
from scripts.harness_compat.cli import exit_code, main
from scripts.harness_compat.contract import Check, Result, Sandbox, VerifiedSet
from scripts.harness_compat.evidence import adapter_digest, cache_root, write_evidence
from scripts.harness_compat.inputs import qualification_inputs
from scripts.harness_compat.publication import prepare_publication
from scripts.harness_compat.report import build_report
from scripts.harness_compat.summary import render_harness, render_overview, workflow_annotations

pytestmark = pytest.mark.usefixtures("isolated_evidence_cache")


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def _repo(root: Path) -> None:
    adapters = root / "scripts/harness_compat/adapters"
    adapters.mkdir(parents=True)
    (root / "scripts/harness_compat/contract.py").write_text("contract\n")
    (adapters / "vibe.py").write_text("adapter\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "compat@example.invalid")
    _git(root, "config", "user.name", "Compat")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "source")


class ScriptedAdapter:
    """Qualifies each release with the next scripted verdict of its queue."""

    name = "vibe"
    probe = "unused:probe"
    stub_help = ""
    green_tests = ("proof",)
    consistency_tests: tuple[str, ...] = ()
    expected_skips: ClassVar[dict[str, str]] = {}
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def __init__(self, verdicts: list[str], releases: tuple[str, ...] = ("3.0.0",)) -> None:
        self.verdicts = verdicts
        self.releases = releases
        self.calls = 0

    def discover(self) -> list[str]:
        return list(self.releases)

    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet("range", floor="2.0.0", ceiling="3.0.0")

    def install(self, sb: Sandbox, version: str) -> Path:
        return sb.bin_dir / version

    def version_line(self, version: str) -> str:
        return version

    def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
        return []

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        verdict = self.verdicts[min(self.calls, len(self.verdicts) - 1)]
        self.calls += 1
        status = {"green": "pass", "red": "fail"}.get(verdict, "error")
        return Result(self.name, version, verdict, (Check("proof", status, f"{verdict} proof"),))  # type: ignore[arg-type]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    waited: list[float] = []
    monkeypatch.setattr(runner.time, "sleep", waited.append)
    return waited


def _result(verdict: str) -> Result:
    return Result("vibe", "1.0.0", verdict)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("verdicts", "final", "attempts", "waited"),
    [
        (["green"], "green", 1, []),
        (["inconclusive", "green"], "green", 2, [30.0]),
        (["inconclusive", "red"], "red", 2, [30.0]),
        (["inconclusive"] * 5, "inconclusive", 3, [30.0, 90.0]),
        (["red"], "red", 1, []),
        (["tooling"], "tooling", 1, []),
    ],
)
def test_only_inconclusive_is_retried_at_most_three_times(
    sleeps: list[float], verdicts: list[str], final: str, attempts: int, waited: list[float]
) -> None:
    queue = iter(verdicts)
    result, _, log = runner._retry(lambda: (_result(next(queue)), None), float("inf"))
    assert (result.verdict, len(log), sleeps) == (final, attempts, waited)


def test_retry_stops_when_the_run_budget_cannot_fit_another_attempt(
    sleeps: list[float], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner.time, "monotonic", lambda: 0.0)
    result, _, log = runner._retry(lambda: (_result("inconclusive"), None), 1.0)
    assert result.verdict == "inconclusive" and sleeps == []
    assert [item["verdict"] for item in log] == ["inconclusive", "not run"]


def test_exhausted_inconclusive_opens_an_issue_without_a_pr(
    tmp_path: Path, sleeps: list[float]
) -> None:
    _repo(tmp_path)
    adapter = ScriptedAdapter(["inconclusive"], releases=("3.0.0", "3.0.1"))
    summary = runner.run_harness(
        adapter, tmp_path, publish=True, run_url="https://example.invalid/1"
    )
    [row] = summary["results"]
    assert [item["verdict"] for item in row["attempts"]] == ["inconclusive"] * 3
    assert adapter.calls == 3 and sleeps == [30.0, 90.0]
    assert summary["deferred"] == ["3.0.1"] and summary["issues"] == [row]
    assert exit_code([summary]) == 2
    report = prepare_publication(summary, tmp_path / "summary.json")
    assert (report.pr_needed, report.issue_action) == (False, "upsert")
    assert "inconclusive" in report.issue_labels and "tooling" not in report.issue_labels
    assert "inconclusive after 3 attempts" in report.issue_body


def test_red_outranks_inconclusive_and_a_green_prefix_still_publishes() -> None:
    green, red, slow = _result("green"), _result("red"), _result("inconclusive")
    summaries = [
        {"issues": [{"verdict": "inconclusive"}, {"verdict": "red"}], "inconclusive": True}
    ]
    assert exit_code(summaries) == 1
    assert exit_code([{"issues": [{"verdict": "inconclusive"}], "inconclusive": True}]) == 2
    assert exit_code([{"issues": [], "inconclusive": False}]) == 0
    report = build_report("vibe", [green, slow], run_url="https://example.invalid/1")
    assert report.pr_needed and report.issue_action == "upsert"
    assert report.issue_labels == ("compat:vibe", "inconclusive")
    assert build_report("vibe", [red, slow], run_url="").issue_labels == (
        "compat:vibe",
        "inconclusive",
    )


def test_a_red_result_revokes_the_cached_green_and_withdraws_the_pr(
    tmp_path: Path, sleeps: list[float]
) -> None:
    _repo(tmp_path)
    adapter = ScriptedAdapter(["red"])
    stale = Result("vibe", "3.0.0", "green", (Check("proof", "pass"),))
    write_evidence(
        tmp_path,
        replace(stale, adapter_digest=adapter_digest(tmp_path, "vibe", adapter)),
        "https://example.invalid/0",
    )
    cached = cache_root(tmp_path) / "vibe/3.0.0.json"
    assert cached.exists()
    summary = runner.run_harness(adapter, tmp_path, version="3.0.0", publish=True)
    assert not cached.exists() and sleeps == []
    assert summary["invalidated"] == ["3.0.0"] and summary["greens"] == []
    assert prepare_publication(summary, tmp_path / "summary.json").pr_withdraw is True


def test_job_summary_annotations_and_overview(tmp_path: Path, sleeps: list[float]) -> None:
    _repo(tmp_path)
    summary = runner.run_harness(ScriptedAdapter(["inconclusive"]), tmp_path)
    text = render_harness(summary)
    assert "### vibe — ⚠️ inconclusive" in text
    assert "inconclusive → inconclusive → inconclusive" in text
    [warning] = workflow_annotations(summary)
    assert warning.startswith("::warning title=Harness compatibility%3A vibe::3.0.0 inconclusive")
    table, missing = render_overview([summary], ["vibe", "pi"])
    assert missing == ["pi"] and "| pi | ❌ no result" in table


def test_summarize_fails_when_a_selected_harness_left_no_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    _repo(tmp_path / "src")
    summary = runner.run_harness(ScriptedAdapter(["green"]), tmp_path / "src")
    results = tmp_path / "results/compat-result-vibe"
    results.mkdir(parents=True)
    (results / "compat-summary.json").write_text(json.dumps({"harnesses": [summary]}))
    target = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    assert main(["summarize", str(tmp_path / "results"), "--expect", "vibe"]) == 0
    assert main(["summarize", str(tmp_path / "results"), "--expect", "vibe,pi"]) == 1
    assert "no result" in target.read_text()


def test_staging_refuses_paths_the_bump_did_not_declare(tmp_path: Path) -> None:
    root, tree = tmp_path / "root", tmp_path / "tree"
    root.mkdir()
    (root / "gate.txt").write_text("1\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "compat@example.invalid")
    _git(root, "config", "user.name", "Compat")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "source")
    _git(tmp_path, "clone", "-q", str(root), str(tree))
    (tree / "gate.txt").write_text("2\n")
    (tree / "workflow.yml").write_text("name: x\n")
    with pytest.raises(RuntimeError, match="did not declare"):
        stage_composed(tree, root, {Path("gate.txt")})
    assert stage_composed(tree, root, {Path("gate.txt"), Path("workflow.yml")}) == [
        Path("gate.txt"),
        Path("workflow.yml"),
    ]


def test_evidence_digest_covers_the_lockfile_and_pytest_settings(tmp_path: Path) -> None:
    _repo(tmp_path)
    (tmp_path / "uv.lock").write_text("lock\n")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    inputs = {path.name for path in qualification_inputs(tmp_path, "vibe")}
    assert {"uv.lock", "pyproject.toml"} <= inputs
