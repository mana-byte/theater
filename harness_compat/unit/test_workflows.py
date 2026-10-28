"""Thin workflow invocation, publication ownership, and independent failure reporting."""

from __future__ import annotations

import re
from pathlib import PurePath
from typing import Any

import pytest
import yaml

from harness_compat.adapters import ADAPTER_NAMES
from harness_compat.policy import policy_relative
from tests.support.paths import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    document = yaml.safe_load((WORKFLOWS / name).read_text())
    document["on"] = document.pop(True)
    return document


@pytest.mark.parametrize(
    "name",
    [
        "harness-compat.yml",
        "harness-compat-verify.yml",
        "harness-compat-canaries.yml",
        "harness-compat-attention.yml",
    ],
)
def test_no_workflow_retains_test_data_or_a_history_cache(name: str) -> None:
    text = (WORKFLOWS / name).read_text()
    for forbidden in (
        "actions/cache",
        "upload-artifact",
        "download-artifact",
        "STEP_SUMMARY",
        # setup-uv's venv cache keys on uv.lock by default; a custom glob could key on anything.
        "cache-dependency-glob",
    ):
        assert forbidden not in text


def test_the_failures_job_runs_last_reads_only_and_cannot_fail_the_run() -> None:
    compat = _load("harness-compat.yml")
    job = compat["jobs"]["failures"]
    # Every other job, so a job added later cannot finish unreported.
    assert set(job["needs"]) == set(compat["jobs"]) - {"failures"}
    assert job["if"] == "${{ always() }}" and job["continue-on-error"] is True
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    assert "secrets." not in yaml.safe_dump(job)
    assert job["steps"][-1]["run"] == "python3 -m harness_compat failures"


def test_dispatch_selects_a_harness_and_scripts_discover_the_versions() -> None:
    compat = _load("harness-compat.yml")
    inputs = compat["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"harness"}
    # Free text: `plan` rejects a name no plugin folder carries.
    assert inputs["harness"]["type"] == "string" and inputs["harness"]["default"] == "all"
    assert 'args=(plan "$REQUESTED_HARNESS")' in compat["jobs"]["plan"]["steps"][-1]["run"]


@pytest.mark.parametrize(
    "name", sorted(path.name for path in WORKFLOWS.glob("harness-compat*.yml"))
)
def test_no_workflow_names_a_harness(name: str) -> None:
    # Harnesses come from the plugin folders through `plan`: adding one edits no workflow.
    names = re.findall(rf"\b(?:{'|'.join(ADAPTER_NAMES)})\b", yaml.safe_dump(_load(name)))
    assert names == []


def test_matrices_come_from_the_plan() -> None:
    compat = _load("harness-compat.yml")
    canaries = _load("harness-compat-canaries.yml")
    matrix = "${{ fromJSON(needs.plan.outputs.harnesses) }}"
    for job in (compat["jobs"]["qualify"], compat["jobs"]["verify"], canaries["jobs"]["canaries"]):
        assert job["strategy"]["matrix"]["harness"] == matrix
    # A PR that touches no harness plans none, and a matrix over nothing fails.
    assert "needs.plan.outputs.harnesses != '[]'" in compat["jobs"]["verify"]["if"]
    assert "python3 -m harness_compat plan" in canaries["jobs"]["plan"]["steps"][-1]["run"]


def test_pr_jobs_are_read_only_and_have_no_automation_secret() -> None:
    compat = _load("harness-compat.yml")
    assert compat["permissions"] == {"contents": "read"}
    assert compat["on"]["pull_request"] is None
    for name in ("static-checks", "plan", "verify"):
        job = compat["jobs"][name]
        assert job.get("permissions", compat["permissions"]) == {"contents": "read"}
        assert "secrets.HARNESS_COMPAT" not in yaml.safe_dump(job)
    reusable = _load("harness-compat-verify.yml")
    assert reusable["permissions"] == {"contents": "read"}
    for job in reusable["jobs"].values():
        assert job.get("permissions", reusable["permissions"]) == {"contents": "read"}
        assert "secrets.HARNESS_COMPAT" not in yaml.safe_dump(job)


@pytest.mark.parametrize("name", ["harness-compat.yml", "harness-compat-attention.yml"])
def test_publication_and_reporting_use_the_compat_token_not_the_pricing_one(name: str) -> None:
    # PR_AUTOMATION_TOKEN belongs to the pricing workflow; compat has its own broader token.
    text = (WORKFLOWS / name).read_text()
    assert "secrets.HARNESS_COMPAT" in text and "PR_AUTOMATION_TOKEN" not in text


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
    reusable = _load("harness-compat-verify.yml")
    # The plan step computes the merge base and the routine decision once and hands both
    # to every per-harness verify call.
    plan = compat["jobs"]["plan"]
    assert plan["outputs"]["base"] == "${{ steps.plan.outputs.base }}"
    assert plan["outputs"]["routine"] == "${{ steps.plan.outputs.routine }}"
    plan_run = plan["steps"][-1]["run"]
    assert 'base="$(git rev-parse HEAD^1)"' in plan_run
    assert "routine=false" in plan_run and "routine=true" in plan_run
    assert 'echo "routine=$routine" >> "$GITHUB_OUTPUT"' in plan_run
    call = compat["jobs"]["verify"]
    assert call["uses"] == "./.github/workflows/harness-compat-verify.yml"
    assert call["with"]["harness"] == "${{ matrix.harness }}"
    assert call["with"]["base"] == "${{ needs.plan.outputs.base }}"
    assert call["with"]["routine"] == "${{ needs.plan.outputs.routine == 'true' }}"
    verify = reusable["jobs"]["verify"]["steps"][-1]["run"]
    assert 'args=(verify "$HARNESS" --base "$BASE" --releases "$VERSIONS")' in verify
    assert "--routine" in verify and "$ROUTINE" in verify
    assert " --check" not in verify
    assert "pull_request_target" not in str(compat)
    assert "pull_request_target" not in str(reusable)


def test_the_floor_range_is_a_repo_variable_wired_into_run_and_verify() -> None:
    compat = _load("harness-compat.yml")
    reusable = _load("harness-compat-verify.yml")
    expression = "${{ vars.HARNESS_COMPAT_MAX_RANGE || '10' }}"
    assert compat["jobs"]["verify"]["with"]["max_range"] == expression
    assert reusable["on"]["workflow_call"]["inputs"]["max_range"]["required"] is True
    qualify = compat["jobs"]["qualify"]
    assert qualify["env"]["MAX_RANGE"] == expression
    assert '--max-range "$MAX_RANGE"' in qualify["steps"][-1]["run"]
    verify = reusable["jobs"]["verify"]
    assert verify["env"]["MAX_RANGE"] == "${{ inputs.max_range }}"
    assert '--max-range "$MAX_RANGE"' in verify["steps"][-1]["run"]


def test_verify_check_names_are_short_and_show_the_version_range() -> None:
    # Check names read "<caller> / <called job>": "Verify codex / Shards" and
    # "Verify codex / 1.4.0-1.7.0" — the shard job name is the range it tests.
    assert _load("harness-compat.yml")["jobs"]["verify"]["name"] == "Verify ${{ matrix.harness }}"
    reusable = _load("harness-compat-verify.yml")
    assert reusable["jobs"]["fanout"]["name"] == "Shards"
    assert reusable["jobs"]["verify"]["name"] == "${{ matrix.range }}"


@pytest.mark.parametrize(
    "name", ["harness-compat.yml", "harness-compat-verify.yml", "harness-compat-canaries.yml"]
)
def test_the_live_suite_runners_can_sandbox_codex(name: str) -> None:
    # Without bubblewrap, and the user namespace Ubuntu 24.04 denies it, codex's workspace-write
    # sandbox is unavailable on Linux and every edit asks for an approval, so the suite would fail
    # codex on a release that is fine.
    text = (WORKFLOWS / name).read_text()
    installs = [line for line in text.splitlines() if "apt-get install" in line]
    assert installs and all("bubblewrap" in line for line in installs)
    assert text.count("apparmor_restrict_unprivileged_userns=0") == len(installs)


def test_pr_checks_diff_against_the_base_the_pr_is_merged_into_not_the_one_it_opened_on() -> None:
    # The event's base SHA is frozen when the PR opens: once the base branch moves, every commit
    # that landed meanwhile looks like part of a policy PR.
    text = (WORKFLOWS / "harness-compat.yml").read_text()
    assert "pull_request.base.sha" not in text
    assert text.count('"$(git rev-parse HEAD^1)"') == 1
    # The verify shards receive that base as an input instead of recomputing it per job.
    reusable = (WORKFLOWS / "harness-compat-verify.yml").read_text()
    assert "git rev-parse HEAD^1" not in reusable


def test_sweeps_interrupt_with_time_left_to_clean_up_and_publish() -> None:
    qualify = _load("harness-compat.yml")["jobs"]["qualify"]
    assert qualify["timeout-minutes"] > 325
    assert "timeout --signal=INT --kill-after=5m 320m" in qualify["steps"][-1]["run"]
    verify = _load("harness-compat-verify.yml")["jobs"]["verify"]
    assert verify["timeout-minutes"] > 325
    assert "timeout --signal=INT --kill-after=5m 320m" in verify["steps"][-1]["run"]


def test_failure_reporter_runs_from_trusted_main_without_project_setup() -> None:
    report = _load("harness-compat-attention.yml")
    # Not the canaries: a red one says the suite missed a planted break, not that a release is bad.
    assert report["on"]["workflow_run"]["workflows"] == ["Harness compatibility"]
    job = report["jobs"]["report"]
    assert "timed_out" in job["if"] and "failure" in job["if"]
    assert job["steps"][0]["with"]["ref"] == "${{ github.sha }}"
    assert job["steps"][-1]["run"] == 'python3 -m harness_compat report "$FAILED_RUN_ID"'
    assert "uv sync" not in yaml.safe_dump(job)


def test_attention_job_dispatch_allows_only_failed_main_release_sweeps() -> None:
    guard = _load("harness-compat-attention.yml")["jobs"]["report"]["if"]
    assert guard == (
        'contains(fromJSON(\'["schedule","workflow_dispatch"]\'), '
        "github.event.workflow_run.event) && "
        "github.event.workflow_run.head_branch == 'main' && "
        'contains(fromJSON(\'["failure","timed_out","cancelled","action_required",'
        '"startup_failure"]\'), github.event.workflow_run.conclusion)'
    )


def test_untrusted_values_are_passed_as_environment_data() -> None:
    for name in (
        "harness-compat.yml",
        "harness-compat-verify.yml",
        "harness-compat-attention.yml",
    ):
        workflow = _load(name)
        for job in workflow["jobs"].values():
            # Reusable-workflow call jobs have no steps; their inputs are data, not script.
            for step in job.get("steps", []):
                assert "${{ github.head_ref }}" not in step.get("run", "")
                assert "${{ github.event" not in step.get("run", "")


def test_canaries_are_workflow_self_checks_for_each_plugin() -> None:
    workflow = _load("harness-compat-canaries.yml")
    job = workflow["jobs"]["canaries"]
    assert 'harness_compat canaries "$HARNESS"' in job["steps"][-1]["run"]
    paths = workflow["on"]["pull_request"]["paths"]
    assert "tests/support/**" in paths
    assert paths.index("!harness_compat/unit/**") > paths.index("harness_compat/**")
    makefile = (REPO_ROOT / "Makefile").read_text()
    for removed in ("compat:", "suite:", "canaries:"):
        assert removed not in makefile


def test_a_policy_gate_pr_does_not_run_the_canaries() -> None:
    # They test the suite, never a release: a gate PR edits only one plugin's policy file.
    paths = _load("harness-compat-canaries.yml")["on"]["pull_request"]["paths"]
    exclusion = "!theater/harness/builtin/plugins/*/compatibility.json"
    assert paths.index(exclusion) > paths.index("theater/**"), "the later pattern wins"
    for name in ADAPTER_NAMES:
        assert PurePath(policy_relative(name)).match(exclusion.removeprefix("!"))
