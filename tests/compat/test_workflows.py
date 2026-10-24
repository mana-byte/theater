"""The workflows keep their promises: nothing persisted, PR checks read-only, one publisher."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from scripts.harness_compat.adapters import ADAPTER_NAMES
from tests.support.paths import REPO_ROOT

WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    document["on"] = document.pop(True)  # YAML reads a bare ``on`` as a boolean
    return document


def _steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return job["steps"]


@pytest.fixture(scope="module")
def compat() -> dict[str, Any]:
    return _load("harness-compat.yml")


@pytest.mark.parametrize("name", ["harness-compat.yml", "harness-compat-canaries.yml"])
def test_nothing_is_cached_or_uploaded(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")

    for forbidden in (
        "actions/cache",
        "upload-artifact",
        "download-artifact",
        "DIAGNOSTICS",
        "STEP_SUMMARY",
        "enable-cache: true",
    ):
        assert forbidden not in text


def test_every_harness_has_one_matrix_leg_and_no_opencode_alias(compat: dict[str, Any]) -> None:
    plan = _steps(compat["jobs"]["plan"])[0]["run"]
    options = compat["on"]["workflow_dispatch"]["inputs"]["harness"]["options"]
    canaries = _load("harness-compat-canaries.yml")["jobs"]["canaries"]

    for name in ADAPTER_NAMES:
        assert f'"{name}"' in plan and name in options
    assert "opencode2" not in plan + str(options)
    assert canaries["strategy"]["matrix"]["harness"] == list(ADAPTER_NAMES)


def test_a_dispatch_discovers_every_release_and_has_no_version_input(
    compat: dict[str, Any],
) -> None:
    inputs = compat["on"]["workflow_dispatch"]["inputs"]

    assert set(inputs) == {"harness"}
    assert "REQUESTED_VERSION" not in str(compat)


def test_pull_request_jobs_have_read_permission_and_no_publishing(compat: dict[str, Any]) -> None:
    text = (WORKFLOWS / "harness-compat.yml").read_text(encoding="utf-8")
    assert compat["permissions"] == {"contents": "read"}
    for job in ("static-checks", "gate-plan", "verify-gate"):
        permissions = compat["jobs"][job].get("permissions", compat["permissions"])
        assert permissions == {"contents": "read"}, job
        body = yaml.safe_dump(compat["jobs"][job])
        for forbidden in ("--publish", "PR_AUTOMATION_TOKEN", "issues", "create-pull-request"):
            assert forbidden not in body, (job, forbidden)
    assert "pull_request_target" not in text


def test_the_gate_pr_check_retests_the_proposed_code_in_check_mode(compat: dict[str, Any]) -> None:
    verify = compat["jobs"]["verify-gate"]
    commands = [step.get("run", "") for step in _steps(verify)]

    assert any('run "$HARNESS" --check' in command for command in commands)
    assert verify["if"] == "needs.gate-plan.outputs.harnesses != '[]'"
    paths = compat["on"]["pull_request"]["paths"]
    for path in ("theater/**", "packages/regie/src/**", "tests/conftest.py", "uv.lock"):
        assert path in paths, "plugin and shared production changes need fresh qualification"
    gates = " ".join(step.get("run", "") for step in _steps(compat["jobs"]["gate-plan"]))
    assert "--routine" in gates and "chore/compat-" in gates


def test_publishing_happens_only_in_the_qualify_job_and_only_on_main(
    compat: dict[str, Any],
) -> None:
    plan = _steps(compat["jobs"]["plan"])[0]["run"]
    qualify = compat["jobs"]["qualify"]
    run = next(step for step in _steps(qualify) if step.get("id") == "compat")

    assert 'if [[ "$REF" == "refs/heads/main" ]]; then publish=true; fi' in plan
    assert 'if [[ "$PUBLISH" == "true" ]]; then' in run["run"] and "--publish" in run["run"]
    assert qualify["permissions"] == {"contents": "read", "issues": "write"}
    assert qualify["timeout-minutes"] <= 360


def test_the_policy_pr_reads_its_body_from_a_file_and_touches_one_path(
    compat: dict[str, Any],
) -> None:
    steps = _steps(compat["jobs"]["qualify"])
    pr = next(step for step in steps if step.get("id") == "pr")
    cleanup = next(step for step in steps if "Remove the temporary PR body" in step.get("name", ""))

    assert pr["with"]["body-path"] == "${{ steps.compat.outputs.pr_body_path }}"
    assert "body" not in pr["with"]
    assert pr["with"]["add-paths"].endswith("/compatibility.json")
    assert pr["if"] == "steps.compat.outputs.pr_action == 'propose'"
    assert cleanup["if"].startswith("always()") and "rm -f" in cleanup["run"]


def test_untrusted_context_reaches_scripts_only_through_environment(compat: dict[str, Any]) -> None:
    for job in compat["jobs"].values():
        for step in _steps(job):
            assert "${{ github.head_ref }}" not in step.get("run", "")
            assert "${{ github.event" not in step.get("run", "")


def test_the_run_keeps_going_to_publish_after_a_red_result_and_then_fails(
    compat: dict[str, Any],
) -> None:
    steps = _steps(compat["jobs"]["qualify"])
    run = next(step for step in steps if step.get("id") == "compat")

    assert run["continue-on-error"] is True
    assert steps[-1]["run"] == "exit 1"
    assert "steps.compat.outcome == 'failure'" in steps[-1]["if"]


def test_the_canary_workflow_watches_the_modules_the_canary_runner_uses() -> None:
    paths = _load("harness-compat-canaries.yml")["on"]["pull_request"]["paths"]

    for module in (
        "canaries",
        "codecopy",
        "cleanup",
        "contract",
        "framework",
        "git",
        "policy",
        "process",
        "qualify",
        "sandbox",
        "suite_runner",
    ):
        assert f"scripts/harness_compat/{module}.py" in paths


def test_every_invocation_rechecks_every_release_with_no_stored_state(
    compat: dict[str, Any],
) -> None:
    text = (WORKFLOWS / "harness-compat.yml").read_text(encoding="utf-8")
    commands = " ".join(step.get("run", "") for step in _steps(compat["jobs"]["qualify"]))

    for stored in ("restore-keys", "--version", "--max-candidates", "known_bad", "OPEN_FAILURES"):
        assert stored not in text, stored
    assert 'args=(run "$HARNESS"' in commands
