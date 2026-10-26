"""Newer-release selection, exact outcomes, and publication before moving on."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.harness_compat import run as run_module
from scripts.harness_compat.cleanup import CleanupFailed
from scripts.harness_compat.contract import Adapter, Check, Result
from scripts.harness_compat.run import (
    DiscoveryError,
    IncompleteRun,
    Run,
    discover_releases,
    run_harness,
    split_shards,
)
from theater.harness.contracts.version_policy import VersionPolicy

POLICY = VersionPolicy("1.0.0", "1.2.0")


class _Adapter:
    name = "claude"

    def __init__(self, releases: list[str]) -> None:
        self.releases = releases

    def discover(self) -> list[str]:
        return self.releases

    def install(self, sb: object, version: str) -> Path:
        raise AssertionError("the runner asks the qualifier, never the installer")


def _run(repo: Path, releases: list[str], verdicts: dict, **kwargs):
    asked: list[str] = []

    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        asked.append(version)
        outcome = verdicts.get(version, "green")
        if isinstance(outcome, BaseException):
            raise outcome
        return Result(adapter.name, version, outcome)

    run = run_harness(repo, _Adapter(releases), qualify=qualify, say=lambda _: None, **kwargs)
    return run, asked


def test_every_newer_release_is_qualified_in_ascending_order(repo: Path) -> None:
    versions = ["0.9.0", "1.0.0", "1.1.0", "1.2.0", "1.3.0", "2.0.0", "2.1.0"]
    run, asked = _run(repo, versions, {})
    assert asked == versions[4:]
    assert run.releases == tuple(asked)
    assert run.policy == POLICY


def test_no_candidate_cap_prefix_or_major_version_is_skipped(repo: Path) -> None:
    versions = ["1.0.0", "1.2.0", *(f"3.{minor}.0" for minor in range(40))]
    _, asked = _run(repo, versions, {})
    assert asked == versions[2:]


def test_all_failure_categories_continue_without_retries(repo: Path) -> None:
    run, asked = _run(
        repo,
        ["1.0.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0"],
        {"1.3.0": "red", "1.4.0": "inconclusive", "1.5.0": "tooling"},
    )
    assert asked == ["1.3.0", "1.4.0", "1.5.0", "1.6.0"]
    assert [result.verdict for result in run.results] == ["red", "inconclusive", "tooling", "green"]


def test_each_result_is_published_before_the_next_qualification(repo: Path) -> None:
    events: list[str] = []

    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        events.append(f"test {version}")
        return Result(adapter.name, version, "green")

    def completed(run: Run) -> None:
        events.append(f"publish {run.results[-1].version}")

    run_harness(
        repo,
        _Adapter(["1.0.0", "1.2.0", "1.3.0", "1.4.0"]),
        qualify=qualify,
        publish=completed,
        say=lambda _: None,
    )
    assert events == ["test 1.3.0", "publish 1.3.0", "test 1.4.0", "publish 1.4.0"]


def test_above_max_exclusions_are_retested_with_newer_releases(repo: Path) -> None:
    (repo / "theater/harness/builtin/plugins/claude/compatibility.json").write_text(
        '{"min":"1.0.0","max":"1.2.0","excluded":["1.3.0"]}\n'
    )
    _, asked = _run(repo, ["1.0.0", "1.2.0", "1.3.0", "1.4.0"], {})
    assert asked == ["1.3.0", "1.4.0"]


def test_no_new_releases_is_a_complete_empty_run(repo: Path) -> None:
    run, asked = _run(repo, ["1.0.0", "1.1.0", "1.2.0"], {})
    assert run.results == run.releases == () and asked == []


@pytest.mark.parametrize("error", [KeyboardInterrupt("deadline"), CleanupFailed("left data")])
def test_interruption_and_cleanup_failure_publish_the_known_version_then_stop(
    repo: Path, error: BaseException
) -> None:
    published: list[Result] = []
    run, asked = _run(
        repo,
        ["1.0.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"],
        {"1.4.0": error},
        publish=lambda run: published.append(run.results[-1]),
    )
    assert asked == ["1.3.0", "1.4.0"]
    assert [result.version for result in published] == asked
    assert published[1].verdict == (
        "inconclusive" if isinstance(error, KeyboardInterrupt) else "tooling"
    )
    assert run.errors


def test_unexpected_qualification_error_is_tooling_and_does_not_skip_later_versions(
    repo: Path,
) -> None:
    run, asked = _run(
        repo,
        ["1.0.0", "1.2.0", "1.3.0", "1.4.0"],
        {"1.3.0": RuntimeError("suite crash")},
    )
    assert asked == ["1.3.0", "1.4.0"]
    assert run.results[0].verdict == "tooling"
    assert "suite crash" in run.results[0].checks[0].detail


def test_publication_errors_remain_visible_while_later_versions_are_tested(repo: Path) -> None:
    def completed(run: Run) -> None:
        raise RuntimeError("GitHub unavailable")

    run, asked = _run(
        repo,
        ["1.0.0", "1.2.0", "1.3.0", "1.4.0"],
        {},
        publish=completed,
    )
    assert asked == ["1.3.0", "1.4.0"]
    assert len(run.errors) == 2 and all("GitHub unavailable" in error for error in run.errors)
    assert [r.verdict for r in run.results] == ["tooling", "tooling"]


def test_a_failed_green_publication_is_excluded_in_the_next_completed_prefix(repo: Path) -> None:
    prefixes: list[Run] = []

    def completed(run: Run) -> None:
        prefixes.append(run)
        if len(prefixes) == 1:
            raise RuntimeError("PR unavailable")

    run, asked = _run(repo, ["1.0.0", "1.2.0", "1.3.0", "1.4.0"], {}, publish=completed)
    assert asked == ["1.3.0", "1.4.0"]
    assert [r.verdict for r in prefixes[1].results] == ["tooling", "green"]
    assert run.results[0].checks[-1].name == "publication"


@pytest.mark.parametrize(
    "found",
    [
        [],
        ["1.0.0", "1.3.0"],
        ["1.2.0", "1.3.0"],
        ["1.0.0", "1.2.0", "1.1.0"],
        ["1.0.0", "1.0.0", "1.2.0"],
        ["1.0.0", "1.2.0", "1.3.0-rc.1"],
    ],
)
def test_incomplete_unordered_duplicated_or_unstable_discovery_is_refused(found: list[str]) -> None:
    with pytest.raises(DiscoveryError):
        discover_releases(_Adapter(found), POLICY)


def test_routine_verification_tests_the_extension_and_added_exclusions_but_no_fresh_head() -> None:
    proposed = VersionPolicy("1.0.0", "1.5.0", ("1.4.0", "1.6.0"))
    found = ["1.0.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"]
    assert discover_releases(_Adapter(found), proposed, base=POLICY, routine=True) == found[2:6]


def test_removing_an_exclusion_must_requalify_the_newly_admitted_version() -> None:
    base = VersionPolicy("1.0.0", "1.2.0", ("1.1.0",))
    assert discover_releases(
        _Adapter(["1.0.0", "1.1.0", "1.2.0", "1.3.0"]), POLICY, base=base, routine=True
    ) == ["1.1.0"]
    with pytest.raises(DiscoveryError, match=r"lacks the policy release 1\.1\.0"):
        discover_releases(_Adapter(["1.0.0", "1.2.0", "1.3.0"]), POLICY, base=base, routine=True)


def test_code_verification_rechecks_current_window_with_no_unrelated_new_releases() -> None:
    found = ["1.0.0", "1.1.0", "1.2.0", "1.3.0"]
    assert discover_releases(_Adapter(found), POLICY, base=POLICY) == found[:3]


@pytest.mark.parametrize(
    "wrong", [Result("codex", "1.3.0", "green"), Result("claude", "1.4.0", "green")]
)
def test_a_wrong_identity_becomes_tooling_for_the_requested_version(
    repo: Path, wrong: Result
) -> None:
    run = run_harness(
        repo,
        _Adapter(["1.0.0", "1.2.0", "1.3.0", "1.4.0"]),
        qualify=lambda *args: wrong,
        say=lambda _: None,
    )
    assert run.results[0].version == "1.3.0" and run.results[0].verdict == "tooling"
    assert len(run.results) == 1 and run.errors


def test_run_accepts_a_completed_prefix_without_claiming_the_rest_was_tested() -> None:
    run = Run(
        "claude", POLICY, ("1.3.0", "1.4.0"), (Result("claude", "1.3.0", "green"),), ("timeout",)
    )
    assert len(run.results) < len(run.releases)


@pytest.mark.parametrize(
    "releases,results",
    [
        (("1.4.0", "1.3.0"), ()),
        (("1.3.0", "1.3.0"), ()),
        (("1.3.0", "1.4.0"), (Result("claude", "1.4.0", "green"),)),
        (("1.3.0",), (Result("codex", "1.3.0", "green"),)),
    ],
)
def test_invalid_runs_fail_closed(releases: tuple, results: tuple) -> None:
    with pytest.raises(IncompleteRun):
        Run("claude", POLICY, releases, results)


def test_shards_split_the_window_into_bounded_explicit_lists() -> None:
    assert split_shards([]) == []
    assert split_shards(["1.3.0"]) == [["1.3.0"]]
    window = [f"1.{minor}.0" for minor in range(3, 22)]
    shards = split_shards(window)
    assert [len(shard) for shard in shards] == [4, 4, 4, 4, 3]
    assert [version for shard in shards for version in shard] == window
    wide = split_shards([f"1.{minor}.0" for minor in range(3, 43)])
    assert len(wide) == 8 and all(len(shard) <= 5 for shard in wide)


def test_an_exhausted_time_budget_records_and_publishes_every_remaining_release(
    repo: Path,
) -> None:
    published: list[Result] = []
    run, asked = _run(
        repo,
        ["1.0.0", "1.2.0", "1.3.0", "1.4.0"],
        {},
        publish=lambda run: published.append(run.results[-1]),
        budget_seconds=0,
    )
    assert asked == []
    assert [result.verdict for result in run.results] == ["inconclusive", "inconclusive"]
    skip = run.results[0].checks[0]
    assert skip.name == "skipped" and "sweep time budget exhausted" in skip.detail
    assert [result.version for result in published] == ["1.3.0", "1.4.0"]


def test_a_generous_time_budget_qualifies_every_release(repo: Path) -> None:
    run, asked = _run(repo, ["1.0.0", "1.2.0", "1.3.0", "1.4.0"], {}, budget_seconds=10_000)
    assert asked == ["1.3.0", "1.4.0"]
    assert all(result.verdict == "green" for result in run.results)


def test_a_budget_that_runs_out_partway_skips_only_the_trailing_releases(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = [0.0]
    monkeypatch.setattr(run_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    asked: list[str] = []

    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        asked.append(version)
        clock[0] += 3000.0
        return Result(adapter.name, version, "green")

    run = run_harness(
        repo,
        _Adapter(["1.0.0", "1.2.0", *(f"1.{minor}.0" for minor in range(3, 10))]),
        qualify=qualify,
        say=lambda _: None,
        budget_seconds=10_000,
    )
    assert asked == ["1.3.0", "1.4.0", "1.5.0", "1.6.0"]
    verdicts = [result.verdict for result in run.results]
    assert verdicts[:4] == ["green"] * 4
    # Skips form a trailing suffix: a skipped release is never followed by a tested one,
    # so every skipped release stays above the highest green, hence above max.
    assert verdicts[4:] == ["inconclusive"] * 3
    assert "sweep time budget exhausted" in run.results[4].checks[0].detail


def test_three_identical_tooling_errors_skip_and_publish_the_rest(repo: Path) -> None:
    published: list[Result] = []
    window = ["1.0.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0"]

    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        return Result(
            adapter.name,
            version,
            "tooling",
            (Check("collect_bundle", "error", f"timed out after 240s on {version}"),),
        )

    run = run_harness(
        repo,
        _Adapter(window),
        qualify=qualify,
        publish=lambda run: published.append(run.results[-1]),
        say=lambda _: None,
    )
    assert [result.verdict for result in run.results] == [
        "tooling",
        "tooling",
        "tooling",
        "inconclusive",
        "inconclusive",
    ]
    skip = run.results[3].checks[0]
    assert skip.name == "skipped"
    assert "skipped after 3 identical collect_bundle tooling errors" in skip.detail
    assert [result.version for result in published] == window[2:]


def test_distinct_tooling_errors_do_not_short_circuit(repo: Path) -> None:
    details = iter(["first failure", "second failure", "third failure", "fourth failure"])

    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        return Result(
            adapter.name, version, "tooling", (Check("qualification", "error", next(details)),)
        )

    run = run_harness(
        repo,
        _Adapter(["1.0.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0"]),
        qualify=qualify,
        say=lambda _: None,
    )
    assert [result.verdict for result in run.results] == ["tooling"] * 4


def test_a_non_tooling_verdict_resets_the_tooling_streak(repo: Path) -> None:
    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        if version == "1.5.0":
            return Result(
                adapter.name, version, "red", (Check("conformance:launch", "fail", "red"),)
            )
        return Result(
            adapter.name, version, "tooling", (Check("qualification", "error", "same failure"),)
        )

    run = run_harness(
        repo,
        _Adapter(["1.0.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0", "1.7.0", "1.8.0"]),
        qualify=qualify,
        say=lambda _: None,
    )
    assert [result.verdict for result in run.results] == [
        "tooling",
        "tooling",
        "red",
        "tooling",
        "tooling",
        "tooling",
    ]
