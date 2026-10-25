"""Thin workflow invocation, publication ownership, and independent failure reporting."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from scripts.harness_compat.adapters import ADAPTER_NAMES
from tests.support.paths import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    document = yaml.safe_load((WORKFLOWS / name).read_text())
    document["on"] = document.pop(True)
    return document


@pytest.mark.parametrize(
    "name", ["harness-compat.yml", "harness-compat-canaries.yml", "harness-compat-attention.yml"]
)
def test_no_workflow_retains_test_data_or_a_history_cache(name: str) -> None:
    text = (WORKFLOWS / name).read_text()
    for forbidden in (
        "actions/cache",
        "upload-artifact",
        "download-artifact",
        "STEP_SUMMARY",
        "enable-cache: true",
    ):
        assert forbidden not in text


def test_dispatch_selects_a_harness_and_scripts_discover_the_versions() -> None:
    compat = _load("harness-compat.yml")
    inputs = compat["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"harness"}
    assert inputs["harness"]["options"] == ["all", *ADAPTER_NAMES]
    assert 'args=(plan "$REQUESTED_HARNESS")' in compat["jobs"]["plan"]["steps"][-1]["run"]


def test_pr_jobs_are_read_only_and_have_no_automation_secret() -> None:
    compat = _load("harness-compat.yml")
    assert compat["permissions"] == {"contents": "read"}
    assert compat["on"]["pull_request"] is None
    for name in ("static-checks", "plan", "verify-gate"):
        job = compat["jobs"][name]
        assert job.get("permissions", compat["permissions"]) == {"contents": "read"}
        assert "PR_AUTOMATION_TOKEN" not in yaml.safe_dump(job)


def test_scripts_own_all_issue_and_pr_operations() -> None:
    for name in (
        "harness-compat.yml",
        "harness-compat-canaries.yml",
        "harness-compat-attention.yml",
    ):
        text = (WORKFLOWS / name).read_text()
        for forbidden in (
            "create-pull-request",
            "gh pr ",
            "gh issue ",
            "--delete-branch",
            "pr_action",
        ):
            assert forbidden not in text
    job = _load("harness-compat.yml")["jobs"]["qualify"]
    assert job["permissions"] == {"contents": "write", "issues": "write", "pull-requests": "write"}
    assert 'harness_compat run "$HARNESS"' in job["steps"][-1]["run"]
    assert job["concurrency"]["cancel-in-progress"] is False


def test_routine_pr_checks_use_the_base_and_stop_at_the_proposed_policy() -> None:
    compat = _load("harness-compat.yml")
    verify = compat["jobs"]["verify-gate"]["steps"][-1]["run"]
    assert 'args=(verify "$HARNESS" --base "$(git rev-parse HEAD^1)")' in verify
    assert "--routine" in verify and "chore/compat-" in verify
    assert " --check" not in verify
    assert "pull_request_target" not in str(compat)


@pytest.mark.parametrize("name", ["harness-compat.yml", "harness-compat-canaries.yml"])
def test_the_live_suite_runners_can_sandbox_codex(name: str) -> None:
    # Without bubblewrap codex's workspace-write sandbox is unavailable on Linux and every edit
    # asks for an approval, so the suite would fail codex on a release that is fine.
    text = (WORKFLOWS / name).read_text()
    installs = [line for line in text.splitlines() if "apt-get install" in line]
    assert installs and all("bubblewrap" in line for line in installs)


def test_pr_checks_diff_against_the_base_the_pr_is_merged_into_not_the_one_it_opened_on() -> None:
    # The event's base SHA is frozen when the PR opens: once the base branch moves, every commit
    # that landed meanwhile looks like part of a policy PR.
    text = (WORKFLOWS / "harness-compat.yml").read_text()
    assert "pull_request.base.sha" not in text
    assert text.count('"$(git rev-parse HEAD^1)"') == 2


@pytest.mark.parametrize("name", ["qualify", "verify-gate"])
def test_sweeps_interrupt_with_time_left_to_clean_up_and_publish(name: str) -> None:
    job = _load("harness-compat.yml")["jobs"][name]
    assert job["timeout-minutes"] > 325
    assert "timeout --signal=INT --kill-after=5m 320m" in job["steps"][-1]["run"]


def test_failure_reporter_runs_from_trusted_main_without_project_setup() -> None:
    report = _load("harness-compat-attention.yml")
    assert report["on"]["workflow_run"]["workflows"] == [
        "Harness compatibility",
        "Harness compatibility canaries",
    ]
    job = report["jobs"]["report"]
    assert "timed_out" in job["if"] and "failure" in job["if"]
    assert job["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    assert job["steps"][-1]["run"] == 'python3 -m scripts.harness_compat report "$FAILED_RUN_ID"'
    assert "uv sync" not in yaml.safe_dump(job)


def test_untrusted_values_are_passed_as_environment_data() -> None:
    for name in ("harness-compat.yml", "harness-compat-attention.yml"):
        workflow = _load(name)
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                assert "${{ github.head_ref }}" not in step.get("run", "")
                assert "${{ github.event" not in step.get("run", "")


def test_canaries_are_workflow_self_checks_for_each_plugin() -> None:
    workflow = _load("harness-compat-canaries.yml")
    job = workflow["jobs"]["canaries"]
    assert job["strategy"]["matrix"]["harness"] == list(ADAPTER_NAMES)
    assert 'harness_compat canaries "$HARNESS"' in job["steps"][-1]["run"]
    assert "tests/support/**" in workflow["on"]["pull_request"]["paths"]
    makefile = (REPO_ROOT / "Makefile").read_text()
    for removed in ("compat:", "suite:", "canaries:"):
        assert removed not in makefile
