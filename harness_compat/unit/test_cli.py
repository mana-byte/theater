"""Workflow-only commands, incremental publication, and bounded PR verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.harness_compat import attention, cli
from scripts.harness_compat.contract import Check, Result
from scripts.harness_compat.git import git
from scripts.harness_compat.policy import derive, write_policy
from scripts.harness_compat.publish import Published
from scripts.harness_compat.run import Run, run_harness
from scripts.harness_compat.summary import render_run
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


def _reporting(monkeypatch: pytest.MonkeyPatch, repo: Path, **source) -> None:
    _publishing(monkeypatch, repo)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    run = {
        "name": f"Harness compatibility ({source.get('event', 'schedule')})",
        "event": "schedule",
        "head_branch": "main",
        "conclusion": "failure",
        **source,
    }

    def api(gh, endpoint):
        assert endpoint == "actions/runs/42"
        return run

    monkeypatch.setattr(attention, "api", api)


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


@pytest.mark.parametrize(
    "outcome,code", [("green", 0), ("red", 0), ("inconclusive", 2), ("tooling", 2)]
)
def test_completed_sweeps_succeed_while_unreliable_outcomes_fail(
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


def test_a_run_summary_ends_with_every_failure_in_one_framed_block_or_with_none() -> None:
    red = Result(
        "claude",
        "1.3.0",
        "red",
        (Check("conformance:resume", "fail", "CAUSE_ONE"), Check("suite", "error", "CAUSE_TWO")),
    )
    releases = ("1.3.0", "1.4.0", "1.5.0")
    run = Run("claude", POLICY, releases, (red, Result("claude", "1.4.0", "green")), ("STOPPED",))

    ledger = render_run(run).partition("compat:failures:begin")[2]

    assert "claude 1.3.0 ended red:" in ledger
    assert "conformance:resume (fail): CAUSE_ONE" in ledger and "suite (error): CAUSE_TWO" in ledger
    assert "error: STOPPED" in ledger and "unfinished: 1.5.0" in ledger
    assert "1.4.0" not in ledger.partition("unfinished")[0]  # a green version is no failure
    assert "compat:failures" not in render_run(_run(Result("claude", "1.3.0", "green")))


def test_main_publishes_completed_prefixes_and_reuses_the_same_run_url(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publishing(monkeypatch, repo)
    asked = _scripted(monkeypatch, ["1.0.0", "1.2.0", "1.3.0", "1.4.0"], {"1.3.0": "red"})
    published: list[Run] = []

    def publish(root, run, *, run_url, max_range=10):
        assert run_url == "https://github.com/o/r/actions/runs/7"
        assert asked == [r.version for r in run.results]
        published.append(run)
        return Published(derive(run.policy, run.results), 17)

    monkeypatch.setattr(cli, "publish", publish)
    assert _main(monkeypatch, repo, "run", "claude") == 0
    assert [len(run.results) for run in published] == [1, 2]
    assert published[-1].results[0].verdict == "red"


@pytest.mark.parametrize("first", ["red", "green"])
def test_a_failed_publication_is_retried_with_all_known_results_before_later_admission(
    repo: Path, monkeypatch: pytest.MonkeyPatch, first: str, capsys
) -> None:
    _publishing(monkeypatch, repo)
    _scripted(monkeypatch, ["1.0.0", "1.2.0", "1.3.0", "1.4.0"], {"1.3.0": first})
    proposals = []

    def publish(root, run, *, run_url, max_range=10):
        proposal = derive(run.policy, run.results)
        proposals.append(proposal)
        if len(proposals) == 1:
            raise RuntimeError("temporary publication failure")
        return Published(proposal, 17)

    monkeypatch.setattr(cli, "publish", publish)
    assert _main(monkeypatch, repo, "run", "claude") == 2
    assert proposals[-1] == VersionPolicy("1.0.0", "1.4.0", ("1.3.0",))
    assert "temporary publication failure" in capsys.readouterr().out


def test_discovery_failure_exits_with_context_for_the_independent_reporter(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _publishing(monkeypatch, repo)

    def broken(*args, **kwargs):
        raise OSError("registry unreachable")

    monkeypatch.setattr(cli, "run_harness", broken)
    assert _main(monkeypatch, repo, "run", "claude") == 2
    output = capsys.readouterr()
    assert "OSError: registry unreachable" in output.err
    assert "compat:start" not in output.out


def test_reporter_failure_is_explicitly_visible_without_fallback_publication(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _reporting(monkeypatch, repo)
    monkeypatch.setattr(
        cli, "report_workflow_failure", lambda *a, **k: (_ for _ in ()).throw(OSError("logs"))
    )

    assert _main(monkeypatch, repo, "report", "42") == 2
    assert "OSError: logs" in capsys.readouterr().err


@pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
def test_main_sweep_reporter_failure_never_creates_a_workflow_issue(
    repo: Path, monkeypatch: pytest.MonkeyPatch, event: str, capsys
) -> None:
    _reporting(monkeypatch, repo, event=event)
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    gh = CompatGithub(git(repo, "rev-parse", "HEAD").strip(), POLICY)

    def broken_report(*args):
        raise OSError("job logs unavailable")

    monkeypatch.setattr(cli, "report_workflow_failure", broken_report)
    assert _main(monkeypatch, repo, "report", "42") == 2
    assert not gh.calls and not gh.issues
    assert "job logs unavailable" in capsys.readouterr().err


@pytest.mark.parametrize(
    "source,error",
    [
        ({"event": "pull_request", "conclusion": "startup_failure"}, OSError("setup failed")),
        ({"event": "pull_request", "conclusion": "timed_out"}, OSError("job logs unavailable")),
        ({"event": "pull_request", "conclusion": "cancelled"}, KeyboardInterrupt()),
        ({"event": "pull_request"}, RuntimeError("GitHub publication unavailable")),
        ({"event": "pull_request", "name": None}, ValueError("unexpected metadata")),
        ({"event": "workflow_dispatch", "head_branch": "feature"}, OSError("logs")),
        ({"event": "push"}, OSError("logs")),
        ({"event": "workflow_run"}, OSError("logs")),
        ({"name": "Unrelated workflow"}, OSError("logs")),
        ({"event": "schedule", "conclusion": "success"}, OSError("logs")),
    ],
)
def test_reporter_exception_fallback_never_mutates_github_without_a_failed_main_sweep(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys, source: dict, error: BaseException
) -> None:
    _reporting(monkeypatch, repo, **source)
    gh = CompatGithub(git(repo, "rev-parse", "HEAD").strip(), POLICY)

    def broken_report(*args):
        raise error

    monkeypatch.setattr(cli, "report_workflow_failure", broken_report)
    assert _main(monkeypatch, repo, "report", "42") == 2
    assert "::error title=Harness compatibility" in capsys.readouterr().out
    assert not gh.calls and not gh.issues and gh.pr is None
    assert set(gh.refs) == {"main"}


def test_source_run_api_failure_is_visible_and_never_authorizes_a_fallback_issue(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _reporting(monkeypatch, repo, event="pull_request")
    requests = []

    def unavailable(gh, endpoint):
        requests.append(endpoint)
        raise OSError("source run API unavailable")

    monkeypatch.setattr(attention, "api", unavailable)
    assert _main(monkeypatch, repo, "report", "42") == 2
    assert requests == ["actions/runs/42"]
    assert "source run API unavailable" in capsys.readouterr().err


def test_pr_source_stops_the_real_reporter_before_job_api_or_publication(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _reporting(monkeypatch, repo, event="pull_request", conclusion="failure")
    assert _main(monkeypatch, repo, "report", "42") == 0


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


def test_shards_emit_the_verify_matrix_with_explicit_version_lists(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    write_policy(repo, "claude", VersionPolicy("1.0.0", "1.6.0"))
    _scripted(monkeypatch, [f"1.{minor}.0" for minor in range(7)], {})
    assert _main(monkeypatch, repo, "shards", "claude", "--base", "main") == 0
    assert json.loads(capsys.readouterr().out) == {
        "include": [
            {"range": "1.0.0-1.3.0", "versions": "1.0.0,1.1.0,1.2.0,1.3.0"},
            {"range": "1.4.0-1.6.0", "versions": "1.4.0,1.5.0,1.6.0"},
        ]
    }


def test_a_shard_is_named_by_its_version_span() -> None:
    assert cli._shard_range(["1.3.0"]) == "1.3.0"
    assert cli._shard_range(["1.3.0", "1.4.0", "1.5.0"]) == "1.3.0-1.5.0"


def test_verify_accepts_the_fanout_shard_version_list(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked = _scripted(monkeypatch, ["1.0.0", "1.1.0", "1.2.0", "1.3.0"], {})
    code = _main(
        monkeypatch, repo, "verify", "claude", "--base", "main", "--releases", "1.1.0,1.2.0"
    )
    assert code == 0
    assert asked == ["1.1.0", "1.2.0"]


def test_verify_refuses_a_shard_list_outside_the_pr_window(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    asked = _scripted(monkeypatch, ["1.0.0", "1.1.0", "1.2.0", "1.3.0"], {})
    code = _main(
        monkeypatch, repo, "verify", "claude", "--base", "main", "--releases", "1.1.0,1.9.0"
    )
    assert code == 2
    assert asked == []
    assert "the shard releases 1.9.0 are outside the PR window" in capsys.readouterr().err


def test_run_passes_the_sweep_time_budget_to_the_runner(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def fake(root, adapter, **kwargs):
        seen.update(kwargs)
        return _run(Result("claude", "1.3.0", "green"))

    monkeypatch.setattr(cli, "run_harness", fake)
    assert _main(monkeypatch, repo, "run", "claude", "--budget-minutes", "310") == 0
    assert seen["budget_seconds"] == 310 * 60


def test_run_passes_the_floor_range_to_publication(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publishing(monkeypatch, repo)
    _scripted(monkeypatch, ["1.0.0", "1.2.0", "1.3.0"], {})
    seen: list[int] = []

    def publish(root, run, *, run_url, max_range):
        seen.append(max_range)
        return Published(derive(run.policy, run.results), 17)

    monkeypatch.setattr(cli, "publish", publish)
    assert _main(monkeypatch, repo, "run", "claude", "--max-range", "5") == 0
    assert seen == [5]


def test_run_refuses_a_floor_range_below_one(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, _run(Result("claude", "1.3.0", "green")))
    assert _main(monkeypatch, repo, "run", "claude", "--max-range", "0") == 2


def test_routine_verify_refuses_a_floor_the_rule_did_not_compute(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    git(repo, "checkout", "-qb", "chore/compat-claude")
    write_policy(repo, "claude", VersionPolicy("1.4.0", "1.7.0"))
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "gate")
    asked = _scripted(monkeypatch, [f"1.{minor}.0" for minor in range(8)], {})

    code = _main(
        monkeypatch, repo, "verify", "claude", "--base", "main", "--routine", "--max-range", "3"
    )

    assert code == 2
    assert asked == []
    assert "expected 1.5.0" in capsys.readouterr().err


def test_routine_verify_runs_the_window_when_the_floor_matches(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git(repo, "checkout", "-qb", "chore/compat-claude")
    write_policy(repo, "claude", VersionPolicy("1.5.0", "1.7.0"))
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "gate")
    asked = _scripted(monkeypatch, [f"1.{minor}.0" for minor in range(8)], {})

    code = _main(
        monkeypatch, repo, "verify", "claude", "--base", "main", "--routine", "--max-range", "3"
    )

    assert code == 0
    assert asked == [f"1.{minor}.0" for minor in range(3, 8)]
