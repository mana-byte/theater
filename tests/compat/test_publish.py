"""Publication: one issue per unsuccessful release, deduped by existence, and one guarded PR."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.harness_compat.contract import Check, Result
from scripts.harness_compat.git import git as read_git
from scripts.harness_compat.policy import PolicyChangeError, load_policy
from scripts.harness_compat.publish import (
    file_issues,
    issue_body,
    issue_title,
    pr_body,
    prepare_proposal,
    publish,
    write_outputs,
)
from scripts.harness_compat.run import Run
from theater.harness.contracts.version_policy import VersionPolicy, version_key

Make = Callable[..., Result]
Git = Callable[..., str]
URL = "https://github.com/o/r/actions/runs/7"


class _Gh:
    """A recording stand-in for the ``gh`` CLI: existing issue titles in, calls out."""

    def __init__(self, existing: tuple[str, ...] = ()) -> None:
        self.existing = existing
        self.calls: list[tuple[str, ...]] = []
        self.bodies: list[str] = []
        self.body_paths: list[Path] = []

    def __call__(self, *args: str) -> str:
        self.calls.append(args)
        if args[0] == "api":
            return "\n".join(self.existing)
        if args[:2] == ("issue", "create"):
            path = Path(args[args.index("--body-file") + 1])
            self.body_paths.append(path)
            self.bodies.append(path.read_text(encoding="utf-8"))
        return ""

    def created(self) -> list[tuple[str, ...]]:
        return [call for call in self.calls if call[:2] == ("issue", "create")]


def _run(results: tuple[Result, ...], policy: VersionPolicy | None = None) -> Run:
    """A run over ``results`` plus a passing outcome for any pinned bound they leave out."""
    policy = policy or VersionPolicy("1.0.0", "1.2.0")
    held = {result.version: result for result in results}
    for bound in (policy.minimum, policy.maximum):
        held.setdefault(bound, Result("claude", bound, "green"))
    ordered = tuple(sorted(held.values(), key=lambda result: version_key(result.version)))
    return Run("claude", policy, tuple(result.version for result in ordered), ordered)


def _context(repo: Path, tmp_path: Path) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_SHA": read_git(repo, "rev-parse", "HEAD").strip(),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUNNER_TEMP": str(tmp_path),
    }


def test_an_issue_names_harness_version_check_and_the_ci_run(result: Make) -> None:
    failing = result("1.1.0", "red", Check("conformance:interrupt", "fail", "turn never stopped"))

    body = issue_body(failing, policy=VersionPolicy("1.0.0", "1.2.0"), run_url=URL)

    assert issue_title("claude", "1.1.0") == "Harness compatibility: claude 1.1.0"
    assert "claude 1.1.0" in body and URL in body
    assert "conformance:interrupt (fail): turn never stopped" in body


def test_an_incompatibility_is_told_apart_from_a_run_that_proved_nothing(result: Make) -> None:
    policy = VersionPolicy("1.0.0", "1.2.0")

    red = issue_body(result("1.1.0", "red"), policy=policy, run_url=URL)
    inconclusive = issue_body(result("1.1.0", "inconclusive"), policy=policy, run_url=URL)
    tooling = issue_body(result("1.1.0", "tooling"), policy=policy, run_url=URL)

    assert "breaks a contract" in red and "No verdict" not in red
    assert "No verdict" in inconclusive and "breaks a contract" not in inconclusive
    assert "not a CLI incompatibility" in tooling


def test_only_unsuccessful_releases_get_issues_with_their_verdict_labels(result: Make) -> None:
    gh = _Gh()
    run = _run(
        (
            result("1.0.0"),
            result("1.1.0", "red"),
            result("1.2.0", "inconclusive"),
            result("1.3.0", "tooling"),
        )
    )

    created, existing = file_issues(run, run_url=URL, gh=gh)

    assert created == [issue_title("claude", v) for v in ("1.1.0", "1.2.0", "1.3.0")]
    assert existing == []
    labels = {
        call[call.index("--title") + 1]: [
            call[i + 1] for i, item in enumerate(call) if item == "--label"
        ]
        for call in gh.created()
    }
    assert labels[issue_title("claude", "1.1.0")] == [
        "compat:claude",
        "incompatible",
        "admitted-regression",
    ]
    assert labels[issue_title("claude", "1.2.0")] == [
        "compat:claude",
        "inconclusive",
        "admitted-regression",
    ]
    assert labels[issue_title("claude", "1.3.0")] == ["compat:claude", "tooling"]


def test_open_issues_are_the_dedupe_and_every_page_is_read(result: Make) -> None:
    gh = _Gh(existing=(issue_title("claude", "1.1.0"), "Harness compatibility failures: claude"))
    run = _run((result("1.1.0", "red"), result("1.3.0", "red")))

    created, existing = file_issues(run, run_url=URL, gh=gh)

    assert created == [issue_title("claude", "1.3.0")]
    assert existing == [issue_title("claude", "1.1.0")]
    listing = next(call for call in gh.calls if call[0] == "api")
    assert "--paginate" in listing and "state=open" in listing
    assert "labels=compat:claude" in listing and "--limit" not in listing


def test_a_clean_run_calls_gh_for_nothing(result: Make) -> None:
    gh = _Gh()

    assert file_issues(_run((result("1.0.0"), result("1.1.0"))), run_url=URL, gh=gh) == ([], [])
    assert gh.calls == []


def test_issue_bodies_travel_in_temporary_files_that_are_removed(result: Make) -> None:
    gh = _Gh()

    file_issues(_run((result("1.1.0", "red"),)), run_url=URL, gh=gh)

    assert gh.bodies and gh.bodies[0].startswith("claude 1.1.0 ended **red**")
    assert not gh.body_paths[0].exists()
    # No call interpolates a body into its arguments.
    assert all(gh.bodies[0] not in " ".join(call) for call in gh.calls)


def test_a_failing_gh_still_removes_the_body_file(result: Make) -> None:
    paths: list[Path] = []

    def gh(*args: str) -> str:
        if args[:2] == ("issue", "create"):
            paths.append(Path(args[args.index("--body-file") + 1]))
            raise RuntimeError("gh issue create failed")
        return ""

    with pytest.raises(RuntimeError, match="gh issue create failed"):
        file_issues(_run((result("1.1.0", "red"),)), run_url=URL, gh=gh)

    assert paths and not paths[0].exists()


def _evidence(result: Make) -> tuple[Result, ...]:
    return (
        result("1.0.0"),
        result("1.1.0", "red"),
        result("1.2.0"),
        result("1.3.0"),
        result("1.4.0", "inconclusive"),
    )


def test_a_proposal_rewrites_only_the_policy_line_and_announces_it(
    repo: Path, result: Make, tmp_path: Path
) -> None:
    output = tmp_path / "output"
    environ = {"GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(tmp_path)}

    published = prepare_proposal(repo, _run(_evidence(result)), run_url=URL, environ=environ)

    assert published.pr_action == "propose"
    assert load_policy(repo, "claude") == VersionPolicy("1.0.0", "1.3.0", ("1.1.0",))
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert lines["pr_action"] == "propose"
    assert lines["pr_title"] == "chore(compat): update claude version policy"
    body = Path(lines["pr_body_path"])
    assert body.parent == tmp_path
    assert body.read_text() == (
        f"claude policy: `>=1.0.0,<=1.2.0` to `>=1.0.0,<=1.3.0 excluding 1.1.0`\n\nRun: {URL}\n"
    )
    body.unlink()


def test_no_proposal_when_the_evidence_agrees_with_the_policy(
    repo: Path, result: Make, tmp_path: Path
) -> None:
    run = _run((result("1.0.0"), result("1.1.0"), result("1.2.0"), result("1.3.0", "red")))
    output = tmp_path / "output"

    published = publish(repo, run, run_url=URL, gh=_Gh(), environ=_context(repo, tmp_path))

    assert published.pr_action == "retire"
    assert output.read_text() == "pr_action=retire\n"


def test_no_passing_release_leaves_open_pull_requests_alone(
    repo: Path, result: Make, tmp_path: Path
) -> None:
    output = tmp_path / "output"
    run = _run((result("1.0.0", "inconclusive"), result("1.1.0", "red"), result("1.2.0", "red")))

    published = publish(repo, run, run_url=URL, gh=_Gh(), environ=_context(repo, tmp_path))

    assert published.pr_action == "keep"
    assert output.read_text() == "pr_action=keep\n"
    assert load_policy(repo, "claude") == VersionPolicy("1.0.0", "1.2.0")


def test_a_dirty_checkout_blocks_the_proposal(repo: Path, result: Make, tmp_path: Path) -> None:
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")

    with pytest.raises(PolicyChangeError, match=r"tracked\.txt"):
        prepare_proposal(
            repo, _run(_evidence(result)), run_url=URL, environ={"RUNNER_TEMP": str(tmp_path)}
        )


def test_a_dirty_checkout_publishes_nothing(repo: Path, result: Make, tmp_path: Path) -> None:
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    gh = _Gh()

    with pytest.raises(PolicyChangeError):
        publish(
            repo, _run(_evidence(result)), run_url=URL, gh=gh,
            environ=_context(repo, tmp_path),
        )  # fmt: skip

    assert gh.calls == []


def test_the_pr_body_is_the_policy_delta_and_the_run_url_only(result: Make) -> None:
    run = _run((result("1.0.0"), result("1.1.0", "red"), result("1.2.0"), result("2.0.0")))

    body = pr_body(run, VersionPolicy("1.0.0", "2.0.0", ("1.1.0",)), run_url=URL)

    assert body == (
        f"claude policy: `>=1.0.0,<=1.2.0` to `>=1.0.0,<=2.0.0 excluding 1.1.0`\n\nRun: {URL}\n"
    )
    assert "|" not in body and "major" not in body.lower()


def test_outputs_are_single_lines_only(tmp_path: Path) -> None:
    target = tmp_path / "out"
    write_outputs(target, {"a": "1", "b": "two words"})

    assert target.read_text() == "a=1\nb=two words\n"
    with pytest.raises(ValueError, match="one line"):
        write_outputs(target, {"body": "line\nforged=1"})


def test_the_outputs_file_content_matches_the_policy_json(
    repo: Path, result: Make, tmp_path: Path
) -> None:
    prepare_proposal(repo, _run(_evidence(result)), run_url=URL, environ=_context(repo, tmp_path))
    text = (repo / "theater/harness/builtin/plugins/claude/compatibility.json").read_text()

    assert text == '{"min":"1.0.0","max":"1.3.0","excluded":["1.1.0"]}\n'
    assert json.loads(text)["max"] == "1.3.0"
    for body in tmp_path.glob("compat-pr-*.md"):
        body.unlink()


def test_a_failing_issue_call_does_not_block_the_proposal_but_is_still_raised(
    repo: Path, result: Make, tmp_path: Path
) -> None:
    def gh(*args: str) -> str:
        raise RuntimeError("gh label create failed")

    output = tmp_path / "output"
    environ = _context(repo, tmp_path)

    with pytest.raises(RuntimeError, match="gh label create failed"):
        publish(repo, _run(_evidence(result)), run_url=URL, gh=gh, environ=environ)

    assert "pr_action=propose" in output.read_text()
    assert load_policy(repo, "claude").maximum == "1.3.0"
    for body in tmp_path.glob("compat-pr-*.md"):
        body.unlink()
