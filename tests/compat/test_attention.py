"""Main sweep failures reach a human; PR failures never publish or recover logs."""

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
    head_branch: str = "main",
    head_repository: str | None = "o/r",
    path: str | None = None,
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
            "Verify claude plugin and policy" if event == "pull_request" else "Qualify claude",
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

    workflow = {
        "id": 42,
        # A run-name appends the event, so the API never reports the bare workflow name.
        "name": f"Harness compatibility ({event})",
        "path": path or ".github/workflows/harness-compat.yml",
        "conclusion": stage,
        "event": event,
        "html_url": URL,
        "head_branch": head_branch,
        "head_sha": "tested-head",
        "head_repository": {"full_name": head_repository} if head_repository else None,
        "pull_requests": [{"number": 7}] if event == "pull_request" else [],
    }

    def api(gh, endpoint):
        assert endpoint == "actions/runs/42", "only source-run metadata may be read"
        return workflow

    def gh(*args):
        assert event != "pull_request", "PR runs must stop before job reads or GitHub mutations"
        assert_gh_flags(args)
        if "--paginate" in args:
            return json.dumps([{"jobs": []}, {"jobs": jobs}])
        assert "--allow-escape-sequences" in args, "gh refuses job logs with colour codes"
        log_reads.append(args[1])
        return logs[args[1]]

    monkeypatch.setattr(attention, "api", api)
    monkeypatch.setattr(
        attention, "file_attention_issue", lambda title, body, **kw: issues.append((title, body))
    )
    monkeypatch.setattr(attention, "publish", lambda root, run, **kw: proposals.append(run))
    attention.report_workflow_failure(repo, 42, gh=gh)
    return issues, proposals, log_reads


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_main_setup_failure_has_a_workflow_issue_and_no_version_proposal(
    repo: Path, monkeypatch, event: str
) -> None:
    issues, proposals, _ = _report(monkeypatch, repo, event=event, log="dependency setup failed")
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


@pytest.mark.parametrize(
    "stage,log",
    [
        ("failure", "dependency setup failed"),
        ("failure", "compat:start claude 1.3.0\ncompat:result claude 1.3.0 red"),
        ("failure", "compat:start claude 1.3.0\ncompat:result claude 1.3.0 inconclusive"),
        ("failure", "compat:start claude 1.3.0\ncompat:result claude 1.3.0 tooling"),
        ("timed_out", "compat:start claude 1.3.0"),
        ("cancelled", "compat:start claude 1.3.0"),
        ("cancelled", "compat:start claude 1.3.0\ncompat:result claude 1.3.0 red"),
        ("action_required", "setup needs approval"),
        ("startup_failure", "runner unavailable"),
    ],
)
def test_pr_failures_never_read_jobs_or_mutate_github(
    repo: Path, monkeypatch, stage: str, log: str
) -> None:
    issues, proposals, log_reads = _report(
        monkeypatch,
        repo,
        event="pull_request",
        stage=stage,
        log=log,
    )
    assert (issues, proposals, log_reads) == ([], [], [])


@pytest.mark.parametrize("event,branch", [("workflow_dispatch", "feature"), ("push", "main")])
def test_runs_outside_main_sweeps_never_publish(
    repo: Path, monkeypatch, event: str, branch: str
) -> None:
    issues, proposals, log_reads = _report(
        monkeypatch,
        repo,
        event=event,
        head_branch=branch,
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 green\n"
        "compat:start claude 1.4.0\ncompat:result claude 1.4.0 tooling",
    )
    assert (issues, proposals, log_reads) == ([], [], [])


def test_pr_mixed_failed_and_cancelled_jobs_never_publish_or_check_supersession(
    repo: Path, monkeypatch
) -> None:
    issues, proposals, log_reads = _report(
        monkeypatch,
        repo,
        event="pull_request",
        stage="failure",
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 red\ncompat:start claude 1.4.0",
        additional_jobs=(
            (
                "Verify codex plugin and policy",
                "cancelled",
                "compat:start codex 1.3.0\ncompat:result codex 1.3.0 tooling\n"
                "compat:start codex 1.4.0",
            ),
            ("Unexpected job", "failure", "malformed recovery log"),
        ),
    )
    assert (issues, proposals, log_reads) == ([], [], [])


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_main_sweep_cancellation_remains_a_failure(repo: Path, monkeypatch, event: str) -> None:
    issues, proposals, _ = _report(
        monkeypatch,
        repo,
        event=event,
        stage="cancelled",
        log="compat:start claude 1.3.0",
    )
    assert issues and proposals[0].results[0].verdict == "inconclusive"


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/harness-compat.yml",
        ".github/workflows/harness-compat.yml@refs/heads/main",
    ],
)
def test_known_workflow_paths_accept_the_optional_api_ref_suffix(
    repo: Path, monkeypatch, path: str
) -> None:
    issues, proposals, log_reads = _report(
        monkeypatch,
        repo,
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
@pytest.mark.parametrize("event", ["schedule", "pull_request"])
def test_foreign_or_unidentified_source_logs_never_authorize_version_exclusions(
    repo: Path, monkeypatch, head_repository: str | None, path: str, event: str
) -> None:
    issues, proposals, log_reads = _report(
        monkeypatch,
        repo,
        event=event,
        head_repository=head_repository,
        path=path,
        log="compat:start claude 1.3.0\ncompat:result claude 1.3.0 red",
    )
    assert not proposals and not log_reads
    if event == "pull_request":
        assert not issues
    else:
        assert issues and "1.3.0" not in issues[0][1]


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
