"""The command line: argument shapes, exit codes and what each command refuses to do."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.harness_compat import cli
from scripts.harness_compat.contract import Check, Result, Verdict
from scripts.harness_compat.policy import write_policy
from scripts.harness_compat.publish import Published
from scripts.harness_compat.run import Run
from theater.harness.contracts.version_policy import VersionPolicy, version_key

Git = Callable[..., str]


def _main(monkeypatch: pytest.MonkeyPatch, root: Path, *argv: str) -> int:
    monkeypatch.chdir(root)
    return cli.main(list(argv))


def _run(verdicts: dict[str, Verdict], policy: VersionPolicy | None = None) -> Run:
    """A run over ``verdicts`` plus a passing outcome for any pinned bound they leave out."""
    policy = policy or VersionPolicy("1.0.0", "1.2.0")
    held: dict[str, Verdict] = {policy.minimum: "green", policy.maximum: "green", **verdicts}
    versions = tuple(sorted(held, key=version_key))
    return Run(
        "claude",
        policy,
        versions,
        tuple(Result("claude", version, held[version]) for version in versions),
    )


@pytest.mark.parametrize(
    ("verdict", "code"), [("green", 0), ("red", 1), ("inconclusive", 2), ("tooling", 2)]
)
def test_qualify_runs_just_the_named_release_and_maps_the_verdict(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    verdict: Verdict, code: int,
) -> None:  # fmt: skip
    seen: list[tuple[str, str, tuple[str, ...]]] = []

    def qualify(root: Path, adapter: object, version: str, *, pytest_args: list[str]) -> Result:
        seen.append((adapter.name, version, tuple(pytest_args)))  # type: ignore[attr-defined]
        return Result("claude", version, verdict, (Check("conformance:turn", "pass", "ok"),))

    monkeypatch.setattr(cli, "qualify_release", qualify)

    assert _main(monkeypatch, repo, "qualify", "claude", "1.7.0", "--", "-k", "mcp") == code
    assert seen == [("claude", "1.7.0", ("-k", "mcp"))]
    assert f"claude 1.7.0: {verdict}" in capsys.readouterr().out


def test_an_unknown_harness_is_a_usage_error(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as stopped:
        cli.main(["qualify", "opencode2", "2.0.18"])

    assert stopped.value.code == 2


def test_suite_runs_only_the_installed_version_in_a_throwaway_sandbox(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    binary = tmp_path / "claude"
    binary.write_text("#!/bin/sh\necho '1.1.0 (Claude Code)'\n")
    binary.chmod(0o755)
    seen: list[tuple[str, str, list[str]]] = []

    def qualify(root: Path, sb: object, name: str, found: Path, version: str, **kwargs) -> Result:
        seen.append((name, version, kwargs["pytest_args"]))
        return Result(name, version, "green")

    monkeypatch.setattr(cli, "qualify_binary", qualify)

    assert _main(monkeypatch, repo, "suite", "claude", "--binary", str(binary), "--", "-x") == 0
    assert seen == [("claude", "1.1.0", ["-x"])]


def test_suite_without_a_binary_says_so(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")

    assert _main(monkeypatch, repo, "suite", "vibe") == 2
    assert "no 'vibe' binary on PATH" in capsys.readouterr().err


def test_publishing_is_reserved_for_github_actions(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(cli, "run_harness", lambda *a, **k: pytest.fail("ran before refusing"))

    assert _main(monkeypatch, repo, "run", "claude", "--publish") == 2
    assert "reserved for GitHub Actions" in capsys.readouterr().err


def test_check_and_publish_exclude_each_other(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit):
        cli.main(["run", "claude", "--check", "--publish"])


def _patch_run(monkeypatch: pytest.MonkeyPatch, run: Run) -> list[object]:
    published: list[object] = []
    monkeypatch.setattr(cli, "run_harness", lambda root, adapter: run)
    monkeypatch.setattr(
        cli,
        "publish",
        lambda root, ran, run_url: (
            published.append((ran, run_url)) or Published("keep", None, (), ())
        ),
    )
    return published


def test_a_plain_run_reports_and_publishes_nothing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    published = _patch_run(monkeypatch, _run({"1.0.0": "green", "1.1.0": "green", "1.3.0": "red"}))

    assert _main(monkeypatch, repo, "run", "claude") == 0
    assert published == []
    out = capsys.readouterr().out
    assert "| 1.3.0 | no | red |" in out


def test_an_admitted_release_that_fails_makes_the_run_fail(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    _patch_run(monkeypatch, _run({"1.0.0": "green", "1.1.0": "red", "1.2.0": "green"}))

    assert _main(monkeypatch, repo, "run", "claude") == 1


def test_a_stale_exclusion_only_fails_the_pr_check(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    policy = VersionPolicy("1.0.0", "1.2.0", ("1.1.0",))
    _patch_run(monkeypatch, _run({"1.0.0": "green", "1.1.0": "green", "1.2.0": "green"}, policy))

    assert _main(monkeypatch, repo, "run", "claude") == 0
    assert _main(monkeypatch, repo, "run", "claude", "--check") == 1


def test_check_passes_when_admission_and_outcome_agree_everywhere(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    policy = VersionPolicy("1.0.0", "1.2.0", ("1.1.0",))
    _patch_run(
        monkeypatch,
        _run({"1.0.0": "green", "1.1.0": "red", "1.2.0": "green", "1.3.0": "red"}, policy),
    )

    assert _main(monkeypatch, repo, "run", "claude", "--check") == 0


def test_publish_runs_only_after_the_whole_run_and_uses_the_workflow_run_url(
    repo: Path, monkeypatch: pytest.MonkeyPatch, git: Git
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    monkeypatch.setenv("GITHUB_SHA", git(repo, "rev-parse", "HEAD").strip())
    run = _run({"1.0.0": "green", "1.2.0": "green"})
    published = _patch_run(monkeypatch, run)

    assert _main(monkeypatch, repo, "run", "claude", "--publish") == 0
    assert published == [(run, "https://github.com/o/r/actions/runs/42")]


def test_a_regression_is_annotated_on_github(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _patch_run(monkeypatch, _run({"1.0.0": "green", "1.1.0": "red", "1.2.0": "green"}))

    assert _main(monkeypatch, repo, "run", "claude") == 1
    assert "::error title=Harness compatibility::claude 1.1.0 is admitted but ended red" in (
        capsys.readouterr().out
    )


def test_an_error_is_exit_two_with_a_message(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(root: Path, adapter: object) -> Run:
        raise OSError("registry unreachable")

    monkeypatch.setattr(cli, "run_harness", boom)

    assert _main(monkeypatch, repo, "run", "claude") == 2
    assert "harness-compat: registry unreachable" in capsys.readouterr().err


def test_gates_lists_the_harnesses_whose_policy_a_change_edits(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], git: Git
) -> None:
    git(repo, "checkout", "-q", "-b", "chore/compat-claude")
    write_policy(repo, "claude", VersionPolicy("1.0.0", "1.5.0"))
    git(repo, "commit", "-qam", "gate")

    assert _main(monkeypatch, repo, "gates", "--base", "main", "--routine") == 0
    assert json.loads(capsys.readouterr().out) == ["claude"]


def test_a_routine_change_that_also_edits_code_fails_the_gates_command(
    repo: Path, monkeypatch: pytest.MonkeyPatch, git: Git, capsys: pytest.CaptureFixture[str]
) -> None:
    git(repo, "checkout", "-q", "-b", "chore/compat-claude")
    write_policy(repo, "claude", VersionPolicy("1.0.0", "1.5.0"))
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    git(repo, "commit", "-qam", "gate and code")

    assert _main(monkeypatch, repo, "gates", "--base", "main", "--routine") == 2
    assert "exactly one compatibility.json" in capsys.readouterr().err
    assert _main(monkeypatch, repo, "gates", "--base", "main") == 0


def test_canaries_default_to_the_policy_maximum(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[str | None] = []

    def fake(root: Path, adapter: object, *, version: str | None) -> Check:
        seen.append(version)
        return Check("canaries", "pass", "ok")

    monkeypatch.setattr(cli, "run_canaries", fake)

    assert _main(monkeypatch, repo, "canaries", "claude") == 0
    assert seen == [None]


def test_results_are_printed_and_never_written_to_a_summary_file(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "step-summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _patch_run(monkeypatch, _run({"1.0.0": "green", "1.2.0": "green"}))

    assert _main(monkeypatch, repo, "run", "claude") == 0
    assert "| 1.2.0 | yes | green |" in capsys.readouterr().out
    assert not summary.exists()


def test_a_plugin_wiring_edit_selects_its_harness_without_the_routine_rules(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], git: Git
) -> None:
    git(repo, "checkout", "-q", "-b", "feature")
    launch = repo / "theater/harness/builtin/plugins/claude/launch.py"
    launch.write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "launch")

    assert _main(monkeypatch, repo, "gates", "--base", "main") == 0
    assert json.loads(capsys.readouterr().out) == ["claude"]
    assert _main(monkeypatch, repo, "gates", "--base", "main", "--routine") == 2
