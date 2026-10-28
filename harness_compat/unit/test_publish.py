"""Version issues and ceiling PRs publish without checkout writes."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from harness_compat.contract import Check, Result
from harness_compat.diagnostics import exception_detail
from harness_compat.git import git
from harness_compat.github.publish import file_issues, issue_body, issue_title, publish
from harness_compat.policy import PolicyChangeError, load_policy, write_policy
from harness_compat.qualification.run import Run
from harness_compat.support.compat_github import POLICY_PATH, CompatGithub, assert_gh_flags
from theater.harness.contracts.version_policy import VersionPolicy

Make = Callable[..., Result]
URL = "https://github.com/o/r/actions/runs/7"
POLICY = VersionPolicy("1.0.0", "1.2.0")


def _run(*results: Result, discovered: tuple[str, ...] = ()) -> Run:
    return Run("claude", POLICY, tuple(r.version for r in results), results, discovered=discovered)


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


def test_publication_raises_the_floor_over_the_discovered_range(repo: Path, result: Make) -> None:
    gh = _github(repo)
    run = _run(result("1.3.0"), discovered=("1.0.0", "1.1.0", "1.2.0", "1.3.0"))

    published = publish(repo, run, run_url=URL, gh=gh, environ=_context(repo), max_range=3)

    assert published.proposal == VersionPolicy("1.1.0", "1.3.0")


def test_passing_newer_version_advances_only_the_shared_json_pr(repo: Path, result: Make) -> None:
    gh = _github(repo)
    published = _publish(repo, gh, result("1.3.0"))
    assert published.proposal == VersionPolicy("1.0.0", "1.3.0") and published.pr == 17
    assert load_policy(repo, "claude") == POLICY and git(repo, "status", "--short") == ""
    _one_line(gh)


@pytest.mark.parametrize("verdict", ["red", "inconclusive", "tooling"])
def test_every_known_newer_failure_gets_only_a_version_issue(
    repo: Path, result: Make, verdict: str
) -> None:
    gh = _github(repo)
    published = _publish(repo, gh, result("1.3.0", verdict))
    assert published.proposal == VersionPolicy("1.0.0", "1.2.0", ("1.3.0",))
    assert issue_title("claude", "1.3.0") in gh.issues and published.pr is None
    assert f"**{verdict}**" in gh.bodies[0]
    assert "binary defect" in gh.bodies[0] if verdict != "red" else "Investigation" in gh.bodies[0]
    assert set(gh.refs) == {"main"} and gh.pr is None
    assert not any("--input" in call and "/git/" in call[1] for call in gh.calls)


def test_failure_at_admitted_max_keeps_the_range_and_excludes_max(repo: Path, result: Make) -> None:
    gh = _github(repo)
    assert _publish(repo, gh, result("1.2.0", "red")).proposal == VersionPolicy(
        "1.0.0",
        "1.2.0",
        ("1.2.0",),
    )


def test_all_failures_create_only_version_issues(repo: Path, result: Make) -> None:
    gh = _github(repo)
    published = _publish(repo, gh, result("1.3.0", "red"), result("1.4.0", "inconclusive"))
    assert published.proposal == VersionPolicy("1.0.0", "1.2.0", ("1.3.0", "1.4.0"))
    assert len(gh.issues) == 2 and gh.pr is None
    assert set(gh.refs) == {"main"}


def test_mixed_results_advance_past_failed_versions_without_admitting_them(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"), result("1.5.0", "tooling"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0", ("1.3.0", "1.5.0"))
    assert len(gh.issues) == 2
    assert gh.pr_title == "chore(compat): claude 1.2.0 → 1.4.0"
    for version in ("1.3.0", "1.5.0"):
        number = gh.issues[issue_title("claude", version)]
        assert f"`{version}`: #{number}" in gh.pr_body
        assert "Ceiling PR: #17" in gh.issue_bodies[number]
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


def test_a_failed_pending_ceiling_retires_the_pr_without_writing_an_exclusion_branch(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.4.0", ("1.3.0",)))
    _publish(repo, gh, result("1.4.0", "inconclusive"))
    assert gh.pr is None and gh.refs["chore/compat-claude"] == "pending"
    assert issue_title("claude", "1.4.0") in gh.issues
    assert not any("--input" in call and "/git/" in call[1] for call in gh.calls)


def test_multiple_runs_reuse_one_pr_and_preserve_earlier_failures(repo: Path, result: Make) -> None:
    gh = _github(repo)
    _publish(repo, gh, result("1.3.0", "red"))
    assert gh.pr is None and set(gh.refs) == {"main"}
    _publish(repo, gh, result("1.4.0"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0", ("1.3.0",))
    assert sum(call[:2] == ("api", "repos/{owner}/{repo}/pulls") for call in gh.calls) == 1
    number = gh.issues[issue_title("claude", "1.3.0")]
    assert "Ceiling PR: #17" in gh.issue_bodies[number]
    assert f"`1.3.0`: #{number}" in gh.pr_body
    _one_line(gh)


def test_failed_pending_ceiling_can_be_replaced_by_a_lower_freshly_qualified_extension(
    repo: Path,
    result: Make,
) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.4.0"))
    _publish(repo, gh, result("1.3.0"), result("1.4.0", "red"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.3.0", ("1.4.0",))
    assert gh.pr_title == "chore(compat): claude 1.2.0 → 1.3.0"
    _one_line(gh)


@pytest.mark.parametrize("existing_pr", [False, True])
def test_exclusion_only_branches_never_keep_or_create_a_pr(
    repo: Path, result: Make, existing_pr: bool
) -> None:
    gh = _github(repo)
    gh.seed_pending(VersionPolicy("1.0.0", "1.2.0", ("1.3.0",)), pr=existing_pr)
    _publish(repo, gh, result("1.3.0", "tooling"))
    assert gh.pr is None and gh.refs["chore/compat-claude"] == "pending"
    assert not any("--input" in call and "/git/" in call[1] for call in gh.calls)


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
    assert any(call[:2] == ("api", "repos/{owner}/{repo}/issues/42") for call in gh.calls)
    assert "unexpected ValueError" in gh.bodies[0] and "not proof" in gh.bodies[0]


def test_all_request_and_body_files_are_removed_even_after_publication_failure(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    gh.fail.add("pr create")
    with pytest.raises(ExceptionGroup):
        _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"))
    assert gh.temporary_paths and all(not path.exists() for path in gh.temporary_paths)
    assert issue_title("claude", "1.3.0") in gh.issues
    gh.fail.clear()
    _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"))
    assert gh.pr == 17
    _one_line(gh)


def test_issue_failure_does_not_drop_exclusions_from_a_ceiling_pr(repo: Path, result: Make) -> None:
    gh = _github(repo)
    gh.fail.add("issue create")
    with pytest.raises(ExceptionGroup) as caught:
        _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"))
    assert "issue create unavailable" in exception_detail(caught.value)
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0", ("1.3.0",)) and gh.pr == 17


@pytest.mark.parametrize("existing_pr", [False, True])
def test_issue_lookup_failure_blocks_all_publication(
    repo: Path, result: Make, existing_pr: bool
) -> None:
    gh = _github(repo)
    if existing_pr:
        gh.seed_pending(VersionPolicy("1.0.0", "1.4.0", ("1.3.0",)))
    gh.issues[issue_title("claude", "1.3.0")] = 42
    refs = dict(gh.refs)
    gh.fail.add("api --paginate")
    run = _run(result("1.4.0"), discovered=("1.0.0", "1.2.0", "1.4.0"))

    with pytest.raises(RuntimeError, match="api --paginate unavailable"):
        publish(repo, run, run_url=URL, gh=gh, environ=_context(repo))

    assert gh.refs == refs and gh.pr == (17 if existing_pr else None)
    assert not gh.requests
    assert len(gh.calls) == 1


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
    assert "must change exactly one line of" in exception_detail(caught.value)
    assert "tracked.txt" in exception_detail(caught.value)
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
    assert published.pr == 17 and not published.created
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


@pytest.mark.parametrize("open_first", [False, True])
def test_paginated_issue_lookup_prefers_open_issues_and_ignores_prs(
    result: Make, open_first: bool
) -> None:
    calls = []
    title = issue_title("claude", "1.3.0")
    issues = [
        {"title": title, "number": 42, "state": "closed"},
        {"title": title, "number": 44, "state": "open"},
    ]
    if open_first:
        issues.reverse()

    def gh(*args):
        assert_gh_flags(args)
        calls.append(args)
        if "--paginate" in args:
            return json.dumps(
                [
                    [issues[0]],
                    [
                        issues[1],
                        {
                            "title": title,
                            "number": 17,
                            "pull_request": {"url": "PR"},
                        },
                    ],
                ]
            )
        return json.dumps({"number": 44, "state": "open", "body": "Human diagnosis"})

    created, existing = file_issues(_run(result("1.3.0", "red")), run_url=URL, gh=gh)
    assert not created and existing == [issue_title("claude", "1.3.0")]
    assert "state=all" in calls[0]
    assert calls[-1][:2] == ("api", "repos/{owner}/{repo}/issues/44")


def test_unrelated_issue_titles_cannot_fence_a_ceiling(repo: Path, result: Make) -> None:
    gh = _github(repo)
    gh.issues = {
        issue_title("codex", "1.3.0"): 42,
        "Harness compatibility: claude not-a-version": 43,
        "Harness compatibility workflow: 1.3.0": 44,
    }
    _publish(repo, gh, result("1.4.0"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0")
    assert not gh.issue_bodies


def test_repeated_failure_reopens_the_same_issue_and_preserves_human_notes(
    repo: Path,
    result: Make,
) -> None:
    gh = _github(repo)
    _publish(repo, gh, result("1.3.0", "red", Check("suite", "fail", "old failure")))
    number = gh.issues[issue_title("claude", "1.3.0")]
    gh.issue_states[number] = "closed"
    gh.issue_bodies[number] = (
        "Diagnosis: adapter bug\n\n" + gh.issue_bodies[number] + "Fix PR: #99\n"
    )
    _publish(
        repo, gh, result("1.3.0", "red", Check("suite", "fail", "new failure")), result("1.4.0")
    )
    body = gh.issue_bodies[number]
    assert len(gh.issues) == 1 and gh.issue_states[number] == "open"
    assert "Diagnosis: adapter bug" in body and "Fix PR: #99" in body
    assert "new failure" in body and "old failure" not in body
    assert body.count("Ceiling PR: #17") == 1


def test_legacy_issue_description_is_preserved_when_managed_sections_are_added(
    repo: Path,
    result: Make,
) -> None:
    gh = _github(repo)
    title = issue_title("claude", "1.3.0")
    gh.issues[title] = 42
    gh.issue_bodies[42] = "Old qualification report\nHuman diagnosis: fixed by #99\n"
    _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"))
    assert gh.issue_bodies[42].startswith("Old qualification report\nHuman diagnosis: fixed by #99")
    assert "Ceiling PR: #17" in gh.issue_bodies[42]


def test_fresh_qualification_can_remove_a_prior_failure_exclusion(repo: Path, result: Make) -> None:
    gh = _github(repo)
    _publish(repo, gh, result("1.3.0", "red"))
    _publish(repo, gh, result("1.3.0"), result("1.4.0"))
    assert gh.policy() == VersionPolicy("1.0.0", "1.4.0")
    number = gh.issues[issue_title("claude", "1.3.0")]
    assert "Ceiling PR: #17" in gh.issue_bodies[number]
    assert f"`1.3.0`: #{number} (green)" in gh.pr_body


def test_floor_pruning_keeps_links_to_the_failed_version_issue(repo: Path, result: Make) -> None:
    gh = _github(repo)
    run = _run(
        result("1.3.0", "red"),
        result("1.4.0"),
        result("1.5.0"),
        discovered=("1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"),
    )
    published = publish(repo, run, run_url=URL, gh=gh, environ=_context(repo), max_range=2)
    number = gh.issues[issue_title("claude", "1.3.0")]
    assert published.proposal == VersionPolicy("1.4.0", "1.5.0")
    assert f"`1.3.0`: #{number} (red)" in gh.pr_body
    assert "Ceiling PR: #17" in gh.issue_bodies[number]


@pytest.mark.parametrize("committed", [False, True])
def test_floor_pruning_keeps_links_to_issues_from_earlier_runs(
    repo: Path, result: Make, committed: bool, git: Callable[..., str]
) -> None:
    version = "1.1.0" if committed else "1.3.0"
    policy = VersionPolicy("1.0.0", "1.2.0", (version,) if committed else ())
    if committed:
        write_policy(repo, "claude", policy)
        git(repo, "commit", "-qam", "retain an earlier exclusion")
    gh = CompatGithub(_context(repo)["GITHUB_SHA"], policy)
    gh.issues[issue_title("claude", version)] = 42
    gh.issue_bodies[42] = "Human diagnosis: adapter bug; fix PR: #99.\n"
    gh.issue_states[42] = "closed"
    results = (result("1.4.0"), result("1.5.0"))
    run = Run(
        "claude",
        policy,
        tuple(r.version for r in results),
        results,
        discovered=("1.0.0", "1.1.0", "1.2.0", "1.4.0", "1.5.0"),
    )

    published = publish(repo, run, run_url=URL, gh=gh, environ=_context(repo), max_range=2)

    assert published.proposal == VersionPolicy("1.4.0", "1.5.0")
    assert f"`{version}`: #42" in gh.pr_body
    assert "Ceiling PR: #17" in gh.issue_bodies[42]
    assert "Human diagnosis: adapter bug; fix PR: #99." in gh.issue_bodies[42]
    assert gh.issue_states[42] == "closed" and not published.created


def test_link_failure_is_retried_without_duplicate_issues_or_lost_exclusions(
    repo: Path,
    result: Make,
) -> None:
    gh = _github(repo)
    gh.fail.add("issue edit")
    with pytest.raises(ExceptionGroup):
        _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"))
    assert gh.pr == 17 and gh.policy().excluded == ("1.3.0",)
    gh.fail.clear()
    _publish(repo, gh, result("1.3.0", "red"), result("1.4.0"))
    assert len(gh.issues) == 1
    assert "Ceiling PR: #17" in gh.issue_bodies[gh.issues[issue_title("claude", "1.3.0")]]
    assert all(not path.exists() for path in gh.temporary_paths)


def test_malformed_managed_section_is_preserved_and_reporting_fails(
    repo: Path, result: Make
) -> None:
    gh = _github(repo)
    _publish(repo, gh, result("1.3.0", "red"))
    number = gh.issues[issue_title("claude", "1.3.0")]
    original = gh.issue_bodies[number].replace("<!-- theater-compat:report:end -->", "Human note")
    gh.issue_bodies[number] = original
    with pytest.raises(ExceptionGroup, match="compatibility publication failed"):
        _publish(repo, gh, result("1.3.0", "red"))
    assert gh.issue_bodies[number] == original and gh.pr is None


def test_issue_explanation_has_the_exact_check_and_run(result: Make) -> None:
    detail = "Expected plugin directory was missing: " + "path component; " * 30 + "final cause"
    body = issue_body(
        result("1.3.0", "red", Check("conformance:interrupt", "fail", detail)),
        policy=POLICY,
        run_url=URL,
    )
    assert f"conformance:interrupt (fail): {detail}" in body and URL in body
