"""Workflow-only commands, incremental publication, and bounded PR verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.harness_compat import cli
from scripts.harness_compat.contract import Check, Result
from scripts.harness_compat.git import git
from scripts.harness_compat.policy import derive, write_policy
from scripts.harness_compat.publish import Published, file_attention_issue
from scripts.harness_compat.run import Run, run_harness
from tests.support.compat_github import CompatGithub
from theater.harness.contracts.version_policy import VersionPolicy

POLICY = VersionPolicy("1.0.0", "1.2.0")


@pytest.fixture(autouse=True)
def _actions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_REF", "refs/pull/1/merge")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "7")


def _main(monkeypatch: pytest.MonkeyPatch, root: Path, *argv: str) -> int:
    monkeypatch.chdir(root)
    return cli.main(list(argv))


def _publishing(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_SHA", git(repo, "rev-parse", "HEAD").strip())


def _run(*results: Result) -> Run:
    return Run("claude", POLICY, tuple(r.version for r in results), results)


def _patch_run(monkeypatch: pytest.MonkeyPatch, run: Run) -> None:
    monkeypatch.setattr(cli, "run_harness", lambda *args, **kwargs: run)


class _Adapter:
    name = "claude"

    def __init__(self, releases: list[str]) -> None:
        self.releases = releases

    def discover(self) -> list[str]:
        return self.releases

    def install(self, *args):
        raise AssertionError("installer was called")


def _scripted(monkeypatch: pytest.MonkeyPatch, releases: list[str], outcomes: dict):
    asked: list[str] = []
    monkeypatch.setattr(cli, "load_adapter", lambda _: _Adapter(releases))

    def qualify(root, adapter, version):
        asked.append(version)
        return Result(adapter.name, version, outcomes.get(version, "green"))

    monkeypatch.setattr(
        cli,
        "run_harness",
        lambda root, adapter, **kwargs: run_harness(root, adapter, qualify=qualify, **kwargs),
    )
    return asked


def test_entry_points_refuse_non_workflow_calls(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS")
    assert _main(monkeypatch, repo, "run", "claude") == 2
    assert "reserved for GitHub workflows" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["qualify", "suite", "gates"])
def test_old_local_entry_points_are_removed(command: str) -> None:
    with pytest.raises(SystemExit):
        cli.main([command, "claude"])


def test_unknown_harness_is_a_usage_error() -> None:
    with pytest.raises(SystemExit):
        cli.main(["run", "opencode2"])


@pytest.mark.parametrize(
    "outcome,code", [("green", 0), ("red", 1), ("inconclusive", 2), ("tooling", 2)]
)
def test_workflow_run_returns_a_failure_for_every_unsuccessful_outcome(
    repo: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, code: int
) -> None:
    _patch_run(monkeypatch, _run(Result("claude", "1.3.0", outcome)))
    assert _main(monkeypatch, repo, "run", "claude") == code


def test_run_prints_results_without_writing_a_summary_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    target = tmp_path / "summary"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    _patch_run(monkeypatch, _run(Result("claude", "1.3.0", "green")))
    assert _main(monkeypatch, repo, "run", "claude") == 0
    assert "| 1.3.0 | no | green |" in capsys.readouterr().out and not target.exists()


def test_main_publishes_completed_prefixes_and_reuses_the_same_run_url(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publishing(monkeypatch, repo)
    asked = _scripted(monkeypatch, ["1.0.0", "1.2.0", "1.3.0", "1.4.0"], {"1.3.0": "red"})
    published: list[Run] = []

    def publish(root, run, *, run_url):
        assert run_url == "https://github.com/o/r/actions/runs/7"
        assert asked == [r.version for r in run.results]
        published.append(run)
        return Published(derive(run.policy, run.results), 17)

    monkeypatch.setattr(cli, "publish", publish)
    assert _main(monkeypatch, repo, "run", "claude") == 1
    assert [len(run.results) for run in published] == [1, 2]
    assert published[-1].results[0].verdict == "red"


@pytest.mark.parametrize("first", ["red", "green"])
def test_a_failed_publication_is_retried_with_all_known_results_before_later_admission(
    repo: Path, monkeypatch: pytest.MonkeyPatch, first: str, capsys
) -> None:
    _publishing(monkeypatch, repo)
    _scripted(monkeypatch, ["1.0.0", "1.2.0", "1.3.0", "1.4.0"], {"1.3.0": first})
    proposals = []
    issues = []

    def publish(root, run, *, run_url):
        proposal = derive(run.policy, run.results)
        proposals.append(proposal)
        if len(proposals) == 1:
            raise RuntimeError("temporary publication failure")
        return Published(proposal, 17)

    monkeypatch.setattr(cli, "publish", publish)
    monkeypatch.setattr(cli, "file_attention_issue", lambda *args: issues.append(args))
    assert _main(monkeypatch, repo, "run", "claude") == 2
    assert proposals[-1] == VersionPolicy("1.0.0", "1.4.0", ("1.3.0",))
    assert not issues and "temporary publication failure" in capsys.readouterr().out


def test_discovery_failure_exits_with_context_for_the_independent_reporter(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _publishing(monkeypatch, repo)
    issues = []

    def broken(*args, **kwargs):
        raise OSError("registry unreachable")

    monkeypatch.setattr(cli, "run_harness", broken)
    monkeypatch.setattr(cli, "file_attention_issue", lambda *args: issues.append(args))
    assert _main(monkeypatch, repo, "run", "claude") == 2
    output = capsys.readouterr()
    assert not issues and "OSError: registry unreachable" in output.err
    assert "compat:start" not in output.out


def test_issue_publication_failure_is_explicitly_visible(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _publishing(monkeypatch, repo)
    monkeypatch.setattr(
        cli, "report_workflow_failure", lambda *a, **k: (_ for _ in ()).throw(OSError("logs"))
    )

    def unavailable(*args):
        raise RuntimeError("GitHub issues unavailable")

    monkeypatch.setattr(cli, "file_attention_issue", unavailable)
    assert _main(monkeypatch, repo, "report", "42") == 2
    assert "Failure issue publication" in capsys.readouterr().err


def test_reporter_failure_comments_on_the_original_workflow_issue(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publishing(monkeypatch, repo)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    gh = CompatGithub(git(repo, "rev-parse", "HEAD").strip(), POLICY)
    title = "Harness compatibility workflow: 42"
    original = "Run: https://github.com/o/r/actions/runs/42\n- Qualify claude: failure\n"

    def broken_report(*args):
        file_attention_issue(title, original, gh=gh)
        raise OSError("job logs unavailable")

    monkeypatch.setattr(cli, "report_workflow_failure", broken_report)
    monkeypatch.setattr(
        cli, "file_attention_issue", lambda *args: file_attention_issue(*args, gh=gh)
    )
    assert _main(monkeypatch, repo, "report", "42") == 2
    assert gh.issue_bodies == {1: original}
    assert len(gh.issues) == 1 and len(gh.issue_comments[1]) == 1
    comment = gh.issue_comments[1][0]
    assert "job logs unavailable" in comment
    assert "Run: https://github.com/o/r/actions/runs/42" in comment
    assert "Reporter: https://github.com/o/r/actions/runs/7" in comment
    assert all(not path.exists() for path in gh.temporary_paths)


def test_routine_check_does_not_depend_on_newer_upstream_heads(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "checkout",
        "-qb",
        "chore/compat-claude",
    )
    write_policy(repo, "claude", VersionPolicy("1.0.0", "1.4.0", ("1.3.0", "1.5.0")))
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "gate")
    asked = _scripted(
        monkeypatch,
        ["1.0.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0"],
        {"1.3.0": "red", "1.5.0": "red"},
    )
    assert _main(monkeypatch, repo, "verify", "claude", "--base", "main", "--routine") == 0
    assert asked == ["1.3.0", "1.4.0", "1.5.0"]


def test_code_verification_fails_on_an_admitted_contract_regression(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _scripted(monkeypatch, ["1.0.0", "1.1.0", "1.2.0", "1.3.0"], {"1.2.0": "red"})
    assert _main(monkeypatch, repo, "verify", "claude", "--base", "main") == 1
    assert asked == ["1.0.0", "1.1.0", "1.2.0"]


def test_the_initial_json_migration_qualifies_its_current_policy_window(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative = "theater/harness/builtin/plugins/claude/compatibility.json"
    git(repo, "rm", relative)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "old gates")
    git(repo, "checkout", "-qb", "migration")
    (repo / relative).parent.mkdir(parents=True, exist_ok=True)
    write_policy(repo, "claude", POLICY)
    git(repo, "add", relative)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "JSON gates")
    asked = _scripted(monkeypatch, ["1.0.0", "1.1.0", "1.2.0", "1.3.0"], {})
    assert _main(monkeypatch, repo, "verify", "claude", "--base", "main") == 0
    assert asked == ["1.0.0", "1.1.0", "1.2.0"]


def test_workflow_plan_selects_requested_or_all_harnesses(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    assert _main(monkeypatch, repo, "plan", "codex") == 0
    assert json.loads(capsys.readouterr().out) == ["codex"]
    assert _main(monkeypatch, repo, "plan") == 0
    assert len(json.loads(capsys.readouterr().out)) == 5


def test_canary_entry_point_is_for_workflow_self_checks(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "run_canaries", lambda *args, **kwargs: Check("canaries", "pass"))
    assert _main(monkeypatch, repo, "canaries", "claude") == 0


def test_report_entry_point_uses_the_supplied_workflow_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = []
    monkeypatch.setattr(
        cli, "report_workflow_failure", lambda root, run_id: seen.append((root, run_id))
    )
    assert _main(monkeypatch, repo, "report", "42") == 0
    assert seen == [(repo, 42)]
