"""Bounded retries, the publication truth table, and what a run shows on GitHub."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

import pytest

from scripts.harness_compat import runner
from scripts.harness_compat.artifacts import stage_composed
from scripts.harness_compat.cli import _open_failures, exit_code, main
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
    monkeypatch.setattr(runner, "_sleep", waited.append)
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


def test_retry_stops_when_the_run_budget_cannot_fit_another_attempt(sleeps: list[float]) -> None:
    soon = time.monotonic() + 1.0  # less than the reserve one more attempt needs
    result, _, log = runner._retry(lambda: (_result("inconclusive"), None), soon)
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


def test_cli_publication_uses_the_run_results_without_changing_external_rows(
    tmp_path: Path, sleeps: list[float]
) -> None:
    _repo(tmp_path)
    state = runner.run_harness_state(ScriptedAdapter(["red"]), tmp_path, version="3.0.0")
    summary = state.to_summary()
    assert state.publication_results[0] is state.rows[0].result
    assert prepare_publication(
        summary, tmp_path / "typed.json", results=state.publication_results
    ) == prepare_publication(summary, tmp_path / "external.json")
    assert summary["results"][0]["checks"] == [
        {"name": "proof", "status": "fail", "detail": "red proof"}
    ]


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


@pytest.mark.parametrize("verdict", ["red", "inconclusive"])
def test_a_final_non_green_revokes_the_cached_green_and_names_the_pr_release(
    tmp_path: Path, sleeps: list[float], verdict: str
) -> None:
    _repo(tmp_path)
    adapter = ScriptedAdapter([verdict])
    stale = Result("vibe", "3.0.0", "green", (Check("proof", "pass"),))
    write_evidence(
        tmp_path,
        replace(stale, adapter_digest=adapter_digest(tmp_path, "vibe", adapter)),
        "https://example.invalid/0",
    )
    cached = cache_root(tmp_path) / "vibe/3.0.0.json"
    assert cached.exists()
    summary = runner.run_harness(adapter, tmp_path, version="3.0.0", publish=True)
    assert not cached.exists()
    assert summary["revoked"] == ["3.0.0"] and summary["greens"] == []
    assert prepare_publication(summary, tmp_path / "summary.json").pr_withdraw == "3.0.0"


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
    red = runner.run_harness(ScriptedAdapter(["red"]), tmp_path / "src", version="3.0.0")
    (results / "compat-summary.json").write_text(json.dumps({"harnesses": [red]}))
    assert main(["summarize", str(tmp_path / "results"), "--expect", "vibe"]) == 1


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


class GateFileAdapter(ScriptedAdapter):
    def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
        (root / "gate.txt").write_text(f"{greens[-1]}\n")
        return [Path("gate.txt")]


@pytest.mark.parametrize(("timeouts", "calls", "verdict"), [(1, 2, None), (9, 3, "inconclusive")])
def test_an_inconclusive_composition_is_retried_then_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    timeouts: int,
    calls: int,
    verdict: str | None,
) -> None:
    (tmp_path / "gate.txt").write_text("old\n")
    _repo(tmp_path)
    seen: list[int] = []

    def slow_check(adapter: object, root: Path, bumped: list[str]) -> list[str]:
        seen.append(1)  # the first call is the candidate's own bump check, then the composition's
        return ["bump check timed out"] if 1 < len(seen) <= timeouts + 1 else []

    monkeypatch.setattr(runner, "check_bump", slow_check)
    summary = runner.run_harness(GateFileAdapter(["green"]), tmp_path)
    assert len(seen) == calls + 1
    assert sleeps == [30.0, 90.0][: calls - 1]
    assert summary["greens"] == ["3.0.0"]
    assert [row["verdict"] for row in summary["issues"]] == ([verdict] if verdict else [])
    if verdict:
        [row] = summary["issues"]
        assert row["role"] == "composition" and len(row["attempts"]) == 3
        report = prepare_publication(summary, tmp_path / "summary.json")
        assert "inconclusive after 3 attempts" in report.issue_body


class UnrelatedFileAdapter(GateFileAdapter):
    """Upstream code ran in the candidate tree and left an extra file behind."""

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        (root / ".github/workflows").mkdir(parents=True)
        (root / ".github/workflows/unrelated.yml").write_text("name: planted\n")
        return super().qualify(root, version, sb, binary)


def test_a_file_outside_the_declared_artifact_roots_never_ships(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "gate.txt").write_text("old\n")
    _repo(tmp_path)
    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: [])
    summary = runner.run_harness(UnrelatedFileAdapter(["green"]), tmp_path, publish=True)
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert summary["published"] is True and staged == ["gate.txt"]


class HungConsistencyAdapter(ScriptedAdapter):
    extra: tuple[Check, ...] = ()

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        hung = Check("consistency", "error", "Command 'pytest' timed out after 900 seconds")
        return Result(self.name, version, "tooling", (hung, *self.extra))


@pytest.mark.parametrize(
    ("extra", "verdict"),
    [
        ((), "inconclusive"),
        ((Check("mock_routes", "fail", "unexpected private mock routes"),), "tooling"),
        ((Check("collect_bundle", "error", "bundle incomplete"),), "tooling"),
    ],
)
def test_only_a_timeout_that_hides_no_deterministic_failure_is_noise(
    tmp_path: Path, extra: tuple[Check, ...], verdict: str
) -> None:
    adapter = HungConsistencyAdapter([])
    adapter.extra = extra
    result, _ = runner._qualify(adapter, tmp_path, "3.0.0", bump=False)
    assert result.verdict == verdict


def test_a_crossed_major_boundary_is_called_out_in_the_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    (tmp_path / "gate.txt").write_text("old\n")
    _repo(tmp_path)
    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: [])
    adapter = GateFileAdapter(["green"], releases=("3.0.0", "4.0.0"))
    summary = runner.run_harness(adapter, tmp_path)
    assert summary["greens"] == ["3.0.0", "4.0.0"]
    report = prepare_publication(summary, tmp_path / "summary.json")
    assert "Major version boundary:** 4.0.0 crosses from 3.x" in report.pr_body


class FailingNewestAdapter(GateFileAdapter):
    """3.0.0 proves green and 3.0.1 red: the first release is promoted, the second revoked."""

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        self.verdicts = ["green" if version == "3.0.0" else "red"]
        self.calls = 0
        return super().qualify(root, version, sb, binary)


def test_a_revoked_release_is_named_even_when_other_releases_are_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    (tmp_path / "gate.txt").write_text("old\n")
    _repo(tmp_path)
    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: [])
    adapter = FailingNewestAdapter([], releases=("3.0.0", "3.0.1"))
    summary = runner.run_harness(adapter, tmp_path)
    assert (summary["greens"], summary["revoked"]) == (["3.0.0"], ["3.0.1"])
    assert prepare_publication(summary, tmp_path / "summary.json").pr_withdraw == "3.0.1"


class WatchedAdapter(ScriptedAdapter):
    """Admits 2.0.0 up to 3.0.0; its watch is the newest release below the ceiling."""

    retired: frozenset[str] = frozenset()

    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet("range", floor="2.0.0", ceiling="3.0.0", known_bad=self.retired)


@pytest.mark.parametrize(
    ("open_failures", "retired", "action", "carried"),
    [
        ((), frozenset(), "close", False),  # a legacy issue without a ledger
        (("2.9.0",), frozenset(), "close", False),  # the watched release itself recovered
        (("2.8.0",), frozenset(), "none", True),  # another release: still unresolved
        (("2.8.0",), frozenset({"2.8.0"}), "close", False),  # retired from the gate by a human
        (("1.0.0",), frozenset(), "close", False),  # below the floor: no longer admitted
        (("2.8.0", "2.9.0"), frozenset(), "ledger", True),  # partial recovery shrinks the list
        (("not-a-release",), frozenset(), "none", True),  # a hand-edited entry never recovers
    ],
)
def test_an_issue_closes_only_when_every_release_it_lists_recovered(
    tmp_path: Path,
    sleeps: list[float],
    open_failures: tuple[str, ...],
    retired: frozenset[str],
    action: str,
    carried: bool,
) -> None:
    _repo(tmp_path)
    adapter = WatchedAdapter(["green"], releases=("2.8.0", "2.9.0"))
    adapter.retired = retired
    summary = runner.run_harness(adapter, tmp_path)
    assert summary["watch"]["version"] == "2.9.0"
    report = prepare_publication(summary, tmp_path / "summary.json", list(open_failures))
    assert report.issue_action == action
    assert ("Still unresolved" in report.issue_body) is carried


def test_a_failing_row_and_an_unretested_failure_share_one_issue_ledger(
    tmp_path: Path, sleeps: list[float]
) -> None:
    _repo(tmp_path)
    summary = runner.run_harness(ScriptedAdapter(["red"]), tmp_path)
    report = prepare_publication(summary, tmp_path / "summary.json", ["2.8.0"])
    assert report.issue_action == "upsert"
    assert "Still unresolved, not retested by this run: `2.8.0`" in report.issue_body
    assert "<!-- compat-failing: 2.8.0,3.0.0 -->" in report.issue_body


def test_a_deterministic_problem_beside_a_timeout_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    (tmp_path / "gate.txt").write_text("old\n")
    _repo(tmp_path)
    seen: list[int] = []

    def mixed(adapter: object, root: Path, bumped: list[str]) -> list[str]:
        seen.append(1)  # the first call is the candidate's own check
        return [] if len(seen) == 1 else ["bump check timed out", "probe 3.0.0: supported=False"]

    monkeypatch.setattr(runner, "check_bump", mixed)
    summary = runner.run_harness(GateFileAdapter(["green"]), tmp_path)
    [row] = summary["issues"]
    assert (row["verdict"], len(row["attempts"]), sleeps) == ("tooling", 1, [])


@pytest.mark.parametrize(
    ("problems", "verdict", "attempts"),
    [
        (["bump check timed out"], "inconclusive", 3),
        (["bump check timed out", "probe 3.0.0: supported=False"], "tooling", 1),
    ],
)
def test_a_candidate_bump_check_retries_only_pure_timeouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sleeps: list[float],
    problems: list[str],
    verdict: str,
    attempts: int,
) -> None:
    (tmp_path / "gate.txt").write_text("old\n")
    _repo(tmp_path)
    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: problems)
    summary = runner.run_harness(GateFileAdapter(["green"]), tmp_path)
    [row] = summary["issues"]
    assert (row["verdict"], len(row["attempts"])) == (verdict, attempts)


@pytest.mark.parametrize(
    ("raw", "listed"),
    [
        ("", []),
        (" ", []),
        ("2.8.0,2.9.0", ["2.8.0", "2.9.0"]),
        ("2.8.0, 2.9.0 ,", ["2.8.0", "2.9.0"]),
    ],
)
def test_the_issue_ledger_tolerates_spaces(
    monkeypatch: pytest.MonkeyPatch, raw: str, listed: list[str]
) -> None:
    monkeypatch.setenv("HARNESS_COMPAT_OPEN_FAILURES", raw)
    assert _open_failures() == listed
