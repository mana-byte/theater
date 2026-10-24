"""Failures and passes accumulate in one remote JSON-only PR without checkout writes."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.harness_compat.contract import Check, Result
from scripts.harness_compat.diagnostics import exception_detail
from scripts.harness_compat.git import git
from scripts.harness_compat.policy import PolicyChangeError, load_policy, write_policy
from scripts.harness_compat.publish import (
    file_attention_issue,
    file_issues,
    issue_body,
    issue_title,
    publish,
)
from scripts.harness_compat.run import Run
from tests.support.compat_github import POLICY_PATH, CompatGithub, assert_gh_flags
from theater.harness.contracts.version_policy import VersionPolicy

Make = Callable[..., Result]
URL = "https://github.com/o/r/actions/runs/7"
POLICY = VersionPolicy("1.0.0", "1.2.0")


def _run(*results: Result) -> Run:
    return Run("claude", POLICY, tuple(r.version for r in results), results)


def _context(repo: Path) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_SHA": git(repo, "rev-parse", "HEAD").strip(),
    }


def _github(repo: Path) -> CompatGithub:
    return CompatGithub(_context(repo)["GITHUB_SHA"], POLICY)


def _publish(repo: Path, gh: CompatGithub, *results: Result):
    return publish(repo, _run(*results), run_url=URL, gh=gh, environ=_context(repo))


def _one_line(gh: CompatGithub) -> None:
    assert gh.changed_files(gh.refs["main"], gh.refs["chore/compat-claude"]) == [
        {"filename": POLICY_PATH, "additions": 1, "deletions": 1},
    ]
    assert not any(call[:2] == ("pr", "close") for call in gh.calls)


def test_passing_newer_version_advances_only_the_shared_json_pr(repo: Path, result: Make) -> None:
    gh = _github(repo)
    published = _publish(repo, gh, result("1.3.0"))
    assert published.proposal == VersionPolicy("1.0.0", "1.3.0") and published.pr == 17
    assert load_policy(repo, "claude") == POLICY and git(repo, "status", "--short") == ""
    _one_line(gh)


@pytest.mark.parametrize("verdict", ["red", "inconclusive", "tooling"])
def test_every_known_newer_failure_gets_an_issue_and_exclusion_only_pr(
    repo: Path, result: Make, verdict: str
) -> None:
    gh = _github(repo)
    published = _publish(repo, gh, result("1.3.0", verdict))
    assert published.proposal == VersionPolicy("1.0.0", "1.2.0", ("1.3.0",))
    assert issue_title("claude", "1.3.0") in gh.issues and published.pr == 17
    assert f"**{verdict}**" in gh.bodies[0]
    assert "binary defect" in gh.bodies[0] if verdict != "red" else "Investigation" in gh.bodies[0]
    _one_line(gh)


def test_failure_at_admitted_max_keeps_the_range_and_excludes_max(repo: Path, result: Make) -> None:
    gh = _github(repo)
    assert _publish(repo, gh, result("1.2.0", "red")).proposal == VersionPolicy(
        "1.0.0",
        "1.2.0",
        ("1.2.0",),
    )


def test_all_failures_still_create_one_policy_pr(repo: Path, result: Make) -> None:
    gh = _github(repo)
    published = _publish(repo, gh, result("1.3.0", "red"), result("1.4.0", "inconclusive"))
    assert published.proposal == VersionPolicy("1.0.0", "1.2.0", ("1.3.0", "1.4.0"))
    assert len(gh.issues) == 2 and gh.pr == 17
    _one_line(gh)


def test_mixed_results_advance_past_failed_versions_without_admitting_them(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"), result("1.5.0", "tooling"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0", ("1.3.0", "1.5.0"))
    assert len(gh.issues) == 2
    _one_line(gh)


def test_existing_pr_keeps_prior_max_and_exclusions_when_new_outcomes_arrive(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.4.0", ("1.3.0", "1.6.0")))
    _publish(repo, gh, result("1.5.0"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.5.0", ("1.3.0", "1.6.0"))
    assert gh.pr == 17 and any(
        call[:2] == ("api", "repos/{owner}/{repo}/pulls/17") for call in gh.calls
    )
    assert not any(call[:2] == ("api", "repos/{owner}/{repo}/pulls") for call in gh.calls)
    _one_line(gh)


def test_a_failed_pending_max_is_excluded_without_shrinking_or_retiring_the_pr(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.4.0", ("1.3.0",)))
    _publish(repo, gh, result("1.4.0", "inconclusive"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0", ("1.3.0", "1.4.0"))
    _one_line(gh)


def test_multiple_runs_reuse_one_pr_and_preserve_earlier_failures(repo: Path, result: Make) -> None:
    gh = _github(repo)
    _publish(repo, gh, result("1.3.0", "red"))
    _publish(repo, gh, result("1.4.0"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0", ("1.3.0",))
    assert sum(call[:2] == ("api", "repos/{owner}/{repo}/pulls") for call in gh.calls) == 1
    _one_line(gh)


def test_matching_pending_change_is_kept_and_an_orphan_branch_gets_its_pr(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.2.0", ("1.3.0",)), pr=False)
    _publish(repo, gh, result("1.3.0", "tooling"))
    assert gh.pr == 17 and gh.refs["chore/compat-claude"] == "pending"


def test_existing_issue_is_updated_with_the_current_observed_cause(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    title = issue_title("claude", "1.3.0")
    gh.issues[title] = 42
    published = _publish(
        repo, gh, result("1.3.0", "tooling", Check("suite", "error", "unexpected ValueError"))
    )
    assert published.existing == (title,) and published.created == ()
    assert any(call[:3] == ("issue", "edit", "42") for call in gh.calls)
    assert "unexpected ValueError" in gh.bodies[0] and "not proof" in gh.bodies[0]


def test_all_request_and_body_files_are_removed_even_after_publication_failure(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.fail.add("pr create")
    with pytest.raises(ExceptionGroup):
        _publish(repo, gh, result("1.3.0", "red"))
    assert gh.temporary_paths and all(not path.exists() for path in gh.temporary_paths)
    assert issue_title("claude", "1.3.0") in gh.issues
    gh.fail.clear()
    _publish(repo, gh, result("1.3.0", "red"))
    assert gh.pr == 17
    _one_line(gh)


def test_issue_failure_does_not_block_the_required_exclusion_pr(repo: Path, result: Make) -> None:
    gh = _github(repo)
    gh.fail.add("issue create")
    with pytest.raises(ExceptionGroup) as caught:
        _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"))
    assert "issue create unavailable" in exception_detail(caught.value)
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0", ("1.3.0",)) and gh.pr == 17


def test_concurrent_branch_updates_are_refused_and_issues_are_still_attempted(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.3.0"))
    gh.fail.add("ref-update")
    with pytest.raises(ExceptionGroup) as caught:
        _publish(repo, gh, result("1.4.0", "red"))
    assert "non-fast-forward" in exception_detail(caught.value)
    assert gh.refs["chore/compat-claude"] == "pending"
    assert issue_title("claude", "1.4.0") in gh.issues


def test_dirty_checkout_blocks_publication_without_touching_github(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    (repo / "tracked.txt").write_text("edited\n")
    with pytest.raises(PolicyChangeError):
        _publish(repo, gh, result("1.3.0"))
    assert gh.calls == []


def test_a_clean_checkout_with_a_different_policy_snapshot_cannot_publish(
    repo: Path, result: Make, git
) -> None:
    write_policy(repo, "claude", VersionPolicy("1.0.0", "1.3.0"))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "advance policy")
    gh = _github(repo)
    with pytest.raises(PolicyChangeError, match="policy changed during qualification"):
        _publish(repo, gh, result("1.4.0"))
    assert not gh.calls


def test_pending_code_changes_are_never_overwritten(repo: Path, result: Make) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.3.0"))
    gh.trees["tree-pending"]["tracked.txt"] = "edited\n"
    with pytest.raises(ExceptionGroup) as caught:
        _publish(repo, gh, result("1.4.0", "red"))
    assert "more than one policy line" in exception_detail(caught.value)
    assert gh.refs["chore/compat-claude"] == "pending"


def test_main_advance_rebases_a_green_proposal_and_preserves_the_pending_policy(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.4.0", ("1.3.0",)))
    previous = gh.refs["main"]
    gh.trees["tree-new-main"] = {**gh.trees["tree-main"], "tracked.txt": "new code\n"}
    gh.commits["new-main"] = {
        "tree": {"sha": "tree-new-main"},
        "parents": [previous],
    }
    gh.refs["main"] = "new-main"
    published = _publish(repo, gh, result("1.5.0"))
    assert published.proposal == VersionPolicy("1.0.0", "1.5.0", ("1.3.0",))
    assert published.pr == 17 and not gh.issues
    _one_line(gh)


def test_clean_run_needs_no_failure_issue_calls(repo: Path, result: Make) -> None:
    gh = _github(repo)
    assert file_issues(_run(result("1.3.0")), run_url=URL, gh=gh) == ([], [])
    assert gh.calls == []


def test_main_advance_never_drops_failures_beside_a_previously_completed_pass(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.refs["main"] = "new-main"
    gh.commits["new-main"] = gh.commits[_context(repo)["GITHUB_SHA"]]
    _publish(repo, gh, result("1.3.0"), result("1.4.0", "tooling"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.3.0", ("1.4.0",))
    assert issue_title("claude", "1.4.0") in gh.issues and gh.pr == 17
    _one_line(gh)


def test_paginated_issue_lookup_flattens_pages_and_does_not_treat_prs_as_issues() -> None:
    calls = []

    def gh(*args):
        assert_gh_flags(args)
        calls.append(args)
        if "--paginate" in args:
            return json.dumps(
                [
                    [],
                    [
                        {"title": "attention", "number": 42},
                        {"title": "attention", "number": 17, "pull_request": {"url": "PR"}},
                    ],
                ]
            )
        return "{}"

    file_attention_issue("attention", "observed failure", gh=gh)
    assert calls[-1][:2] == ("api", "repos/{owner}/{repo}/issues/42/comments")


def test_issue_explanation_has_the_exact_check_and_run(result: Make) -> None:
    body = issue_body(
        result("1.3.0", "red", Check("conformance:interrupt", "fail", "turn never stopped")),
        policy=POLICY,
        run_url=URL,
    )
    assert "conformance:interrupt (fail): turn never stopped" in body and URL in body
