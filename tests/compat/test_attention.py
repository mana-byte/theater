"""Failures before or after the suite still reach a human, without retained test reports."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.harness_compat import attention
from scripts.harness_compat.codecopy import code_copy
from scripts.harness_compat.git import git
from scripts.harness_compat.policy import policy_relative
from scripts.harness_compat.run import Run
from tests.support.compat_github import assert_gh_flags
from tests.support.paths import REPO_ROOT

URL = "https://github.com/o/r/actions/runs/42"


def test_only_known_unpublished_versions_are_recovered_with_no_log_authorized_passes() -> None:
    log = "\n".join(
        [
            "2026-10-24T00:00Z compat:start claude 1.3.0",
            "2026-10-24T00:00Z compat:result claude 1.3.0 green",
            "2026-10-24T00:00Z compat:published claude 1.3.0",
            "2026-10-24T00:00Z compat:start claude 1.4.0",
            "2026-10-24T00:00Z compat:result claude 1.4.0 red",
            "2026-10-24T00:00Z compat:start claude 1.5.0",
            "2026-10-24T00:00Z compat:result claude 1.5.0 green",
            "2026-10-24T00:00Z compat:start claude 1.6.0",
            "2026-10-24T00:00Z compat:result codex 1.7.0 red",
            "2026-10-24T00:00Z compat:result claude 9.9.9 red",
        ]
    )
    recovered = attention.recover_failures(log, "claude", run_url=URL)
    assert [(r.version, r.verdict) for r in recovered] == [
        ("1.4.0", "red"),
        ("1.5.0", "tooling"),
        ("1.6.0", "inconclusive"),
    ]
    assert all(URL in r.checks[0].detail for r in recovered)


@pytest.mark.parametrize("log", ["", "setup failed", "compat:result claude 1.3.0 red"])
def test_global_failures_never_invent_a_version(log: str) -> None:
    assert attention.recover_failures(log, "claude", run_url=URL) == ()


def _report(
    monkeypatch,
    repo: Path,
    *,
    log: str,
    event: str = "schedule",
    stage: str = "failure",
    job: str | None = None,
    head_branch: str = "main",
    head_repository: str | None = "o/r",
    path: str | None = None,
    superseded: bool = False,
    additional_jobs: tuple[tuple[str, str, str], ...] = (),
):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_SHA", git(repo, "rev-parse", "HEAD").strip())
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    issues: list[tuple[str, str]] = []
    proposals: list[Run] = []
    log_reads: list[str] = []
    job_specs = [
        (
            job
            or ("Verify claude plugin and policy" if event == "pull_request" else "Qualify claude"),
            stage,
            log,
        ),
        *additional_jobs,
    ]
    jobs = [
        {"name": label, "conclusion": conclusion, "id": identifier}
        for identifier, (label, conclusion, _) in enumerate(job_specs, start=3)
    ]
    logs = {
        f"repos/{{owner}}/{{repo}}/actions/jobs/{identifier}/logs": detail
        for identifier, (_, _, detail) in enumerate(job_specs, start=3)
    }

    canaries = bool(job and job.startswith("Canaries "))
    workflow = {
        "id": 42,
        "workflow_id": 9,
        "name": "Harness compatibility canaries" if canaries else "Harness compatibility",
        "path": path or f".github/workflows/harness-compat{'-canaries' if canaries else ''}.yml",
        "conclusion": stage,
        "event": event,
        "html_url": URL,
        "head_branch": head_branch,
        "head_sha": "tested-head",
        "head_repository": {"full_name": head_repository} if head_repository else None,
        "pull_requests": [{"number": 7}] if event == "pull_request" else [],
    }

    def api(gh, endpoint):
        if endpoint.startswith("pulls/"):
            return {
                "state": "open",
                "head": {"sha": "new-head" if superseded else "tested-head"},
            }
        if endpoint.startswith("actions/workflows/"):
            return {
                "workflow_runs": [
                    {
                        **workflow,
                        "id": 43 if superseded else 42,
                        "path": workflow["path"].partition("@")[0],
                    }
                ]
            }
        return workflow

    def gh(*args):
        assert_gh_flags(args)
        if "--paginate" in args:
            return json.dumps([{"jobs": []}, {"jobs": jobs}])
        log_reads.append(args[1])
        return logs[args[1]]

    monkeypatch.setattr(attention, "api", api)
    monkeypatch.setattr(
        attention, "file_attention_issue", lambda title, body, **kw: issues.append((title, body))
    )
    monkeypatch.setattr(attention, "publish", lambda root, run, **kw: proposals.append(run))
    attention.report_workflow_failure(repo, 42, gh=gh)
    return issues, proposals, log_reads


def test_setup_failure_has_a_workflow_issue_and_no_version_proposal(
    repo: Path, monkeypatch
) -> None:
    issues, proposals, _ = _report(monkeypatch, repo, log="dependency setup failed")
    assert len(issues) == 1 and "Human attention" in issues[0][1] and URL in issues[0][1]
    assert proposals == []


def test_hosted_timeout_recovers_known_version_as_inconclusive_exclusion(
    repo: Path, monkeypatch
) -> None:
    issues, proposals, _ = _report(
        monkeypatch, repo, log="compat:start claude 1.3.0", stage="timed_out"
    )
    assert issues and len(proposals) == 1
    assert [(r.version, r.verdict) for r in proposals[0].results] == [("1.3.0", "inconclusive")]


def test_completed_failure_is_recovered_if_publication_crashed(repo: Path, monkeypatch) -> None:
    _, proposals, _ = _report(
        monkeypatch,
        repo,
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 tooling",
    )
    assert proposals[0].results[0].verdict == "tooling"


@pytest.mark.parametrize("verdict", ["red", "inconclusive", "tooling"])
def test_pr_failures_get_issues_and_exclusion_proposals_from_trusted_code(
    repo: Path, monkeypatch, verdict: str
) -> None:
    issues, proposals, log_reads = _report(
        monkeypatch,
        repo,
        event="pull_request",
        log=f"compat:start claude 1.3.0\ncompat:result claude 1.3.0 {verdict}",
    )
    assert issues and log_reads and len(proposals) == 1
    assert [(r.version, r.verdict) for r in proposals[0].results] == [("1.3.0", verdict)]


def test_verification_greens_need_no_publication_and_unfinished_versions_are_inconclusive() -> None:
    log = "compat:start claude 1.3.0\ncompat:result claude 1.3.0 green\ncompat:start claude 1.4.0"
    results = attention.recover_failures(log, "claude", run_url=URL, publication_required=False)
    assert [(r.version, r.verdict) for r in results] == [("1.4.0", "inconclusive")]


def test_dispatch_from_a_feature_branch_reports_failures_without_authorizing_greens(
    repo: Path, monkeypatch
) -> None:
    issues, proposals, _ = _report(
        monkeypatch,
        repo,
        event="workflow_dispatch",
        head_branch="feature",
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 green\n"
        "compat:start claude 1.4.0\ncompat:result claude 1.4.0 tooling",
    )
    assert issues and [(r.version, r.verdict) for r in proposals[0].results] == [
        ("1.4.0", "tooling")
    ]


def test_canary_failures_report_the_known_version_as_suite_tooling(repo: Path, monkeypatch) -> None:
    issues, proposals, _ = _report(
        monkeypatch,
        repo,
        job="Canaries claude",
        log="compat:start claude 1.2.0\ncompat:result claude 1.2.0 tooling",
    )
    assert issues and proposals[0].results[0].verdict == "tooling"


@pytest.mark.parametrize(
    "event,job,path",
    [
        ("pull_request", None, None),
        ("schedule", "Canaries claude", None),
        ("workflow_dispatch", "Canaries claude", None),
        (
            "schedule",
            "Canaries claude",
            ".github/workflows/harness-compat-canaries.yml@refs/heads/main",
        ),
    ],
)
def test_supersession_never_manufactures_an_unfinished_version_failure(
    repo: Path, monkeypatch, event: str, job: str | None, path: str | None
) -> None:
    log = "compat:start claude 1.3.0"
    if job is None:
        log += "\ncompat:result claude 1.3.0 green\ncompat:start claude 1.4.0"
    issues, proposals, _ = _report(
        monkeypatch,
        repo,
        event=event,
        job=job,
        stage="cancelled",
        superseded=True,
        log=log,
        path=path,
    )
    assert not issues and not proposals


def test_mixed_failed_and_superseded_matrix_jobs_only_exclude_genuine_unfinished_failures(
    repo: Path, monkeypatch, git
) -> None:
    codex = repo / policy_relative("codex")
    codex.parent.mkdir(parents=True)
    codex.write_text((repo / policy_relative("claude")).read_text())
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "codex policy")
    issues, proposals, _ = _report(
        monkeypatch,
        repo,
        event="pull_request",
        stage="failure",
        superseded=True,
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 red\ncompat:start claude 1.4.0",
        additional_jobs=(
            (
                "Verify codex plugin and policy",
                "cancelled",
                "compat:start codex 1.3.0\ncompat:result codex 1.3.0 tooling\n"
                "compat:start codex 1.4.0",
            ),
        ),
    )
    outcomes = [(run.harness, [(r.version, r.verdict) for r in run.results]) for run in proposals]
    assert issues and outcomes == [
        ("claude", [("1.3.0", "red"), ("1.4.0", "inconclusive")]),
        ("codex", [("1.3.0", "tooling")]),
    ]


def test_a_completed_failure_survives_supersession_without_excluding_unfinished_versions(
    repo: Path, monkeypatch
) -> None:
    issues, proposals, _ = _report(
        monkeypatch,
        repo,
        event="pull_request",
        stage="cancelled",
        superseded=True,
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 red\ncompat:start claude 1.4.0",
    )
    assert not issues and [(r.version, r.verdict) for r in proposals[0].results] == [
        ("1.3.0", "red")
    ]


@pytest.mark.parametrize("job", [None, "Canaries claude"])
def test_an_ordinary_cancellation_still_needs_attention_and_a_known_version_exclusion(
    repo: Path, monkeypatch, job: str | None
) -> None:
    issues, proposals, _ = _report(
        monkeypatch,
        repo,
        event="pull_request",
        job=job,
        stage="cancelled",
        log="compat:start claude 1.3.0",
    )
    assert issues and proposals[0].results[0].verdict == "inconclusive"


def test_main_sweep_cancellation_is_a_failure_even_when_a_newer_run_exists(
    repo: Path, monkeypatch
) -> None:
    issues, proposals, _ = _report(
        monkeypatch,
        repo,
        stage="cancelled",
        superseded=True,
        log="compat:start claude 1.3.0",
    )
    assert issues and proposals[0].results[0].verdict == "inconclusive"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/harness-compat.yml",
        ".github/workflows/harness-compat.yml@refs/pull/7/head",
    ],
)
def test_known_workflow_paths_accept_the_optional_api_ref_suffix(
    repo: Path, monkeypatch, path: str
) -> None:
    issues, proposals, log_reads = _report(
        monkeypatch,
        repo,
        event="pull_request",
        path=path,
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 red",
    )
    assert issues and log_reads and proposals[0].results[0].version == "1.3.0"


@pytest.mark.parametrize(
    "head_repository,path",
    [
        ("fork/r", ".github/workflows/harness-compat.yml"),
        (None, ".github/workflows/harness-compat.yml"),
        ("o/r", ".github/workflows/foreign.yml"),
    ],
)
def test_foreign_or_unidentified_source_logs_cannot_propose_version_exclusions(
    repo: Path, monkeypatch, head_repository: str | None, path: str
) -> None:
    issues, proposals, log_reads = _report(
        monkeypatch,
        repo,
        event="pull_request",
        head_repository=head_repository,
        path=path,
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 red",
    )
    assert issues and not proposals and not log_reads
    assert "1.3.0" not in issues[0][1]


def test_reporting_imports_without_project_dependencies_or_site_packages() -> None:
    with code_copy(REPO_ROOT) as tree:
        result = subprocess.run(
            [sys.executable, "-S", "-c", "import scripts.harness_compat.cli"],
            cwd=tree,
            env={**os.environ, "PYTHONPATH": str(tree), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
    assert result.returncode == 0, result.stderr
