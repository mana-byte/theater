"""One harness's run: every release from the pinned minimum, sequentially, failing closed."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.harness_compat.contract import Adapter, Result, Verdict
from scripts.harness_compat.run import (
    DiscoveryError,
    IncompleteRun,
    Run,
    discover_releases,
    run_harness,
)
from theater.harness.contracts.version_policy import VersionPolicy

Make = Callable[..., Result]


class _Adapter:
    name = "claude"

    def __init__(self, releases: list[str]) -> None:
        self.releases = releases

    def discover(self) -> list[str]:
        return list(self.releases)

    def install(self, sb: object, version: str) -> Path:
        raise AssertionError("the run asks the qualifier, never the installer")


def _qualifier(
    verdicts: dict[str, list[Verdict] | Verdict],
) -> tuple[Callable[[Path, Adapter, str], Result], list[str]]:
    asked: list[str] = []
    queued = {
        version: ([item] if isinstance(item, str) else list(item))
        for version, item in verdicts.items()
    }

    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        asked.append(version)
        script = queued.get(version, ["green"])
        verdict = script.pop(0) if len(script) > 1 else script[0]
        return Result(adapter.name, version, verdict)

    return qualify, asked


def _run(repo: Path, releases: list[str], verdicts: dict, **kwargs: object):
    qualify, asked = _qualifier(verdicts)
    seen: list[str] = []
    run = run_harness(
        repo,
        _Adapter(releases),
        qualify=qualify,
        say=seen.append,
        **kwargs,  # type: ignore[arg-type]
    )
    return run, asked, seen


def test_every_release_from_the_minimum_up_is_qualified_in_ascending_order(repo: Path) -> None:
    releases = ["0.9.0", "1.0.0", "1.0.1", "1.1.0", "1.2.0", "1.3.0", "2.0.0", "2.1.0"]

    run, asked, _ = _run(repo, releases, {})

    assert asked == releases[1:]
    assert [result.version for result in run.results] == releases[1:]
    assert run.policy == VersionPolicy("1.0.0", "1.2.0")
    assert run.harness == "claude"


def test_releases_beyond_any_cap_prefix_or_line_are_all_tested(repo: Path) -> None:
    releases = ["1.0.0", "1.2.0", *(f"3.{minor}.0" for minor in range(40))]

    _, asked, _ = _run(repo, releases, {})

    assert asked == releases


def test_a_failure_never_stops_the_run_or_skips_a_later_release(repo: Path) -> None:
    releases = ["1.0.0", "1.1.0", "1.2.0", "1.3.0"]

    run, asked, _ = _run(repo, releases, {"1.1.0": "red", "1.2.0": "tooling"})

    assert asked == releases
    assert [result.verdict for result in run.results] == ["green", "red", "tooling", "green"]


def test_previously_admitted_and_excluded_releases_are_retested_every_time(repo: Path) -> None:
    (repo / "theater/harness/builtin/plugins/claude/compatibility.json").write_text(
        '{"min":"1.0.0","max":"1.2.0","excluded":["1.1.0"]}\n', encoding="utf-8"
    )

    _, asked, _ = _run(repo, ["1.0.0", "1.1.0", "1.2.0"], {})

    assert asked == ["1.0.0", "1.1.0", "1.2.0"]


def test_an_inconclusive_release_gets_one_fresh_attempt_and_the_last_word(repo: Path) -> None:
    run, asked, seen = _run(
        repo,
        ["1.0.0", "1.1.0", "1.2.0"],
        {"1.1.0": ["inconclusive", "green"], "1.2.0": ["inconclusive", "inconclusive"]},
    )

    assert asked == ["1.0.0", "1.1.0", "1.1.0", "1.2.0", "1.2.0"]
    assert [result.verdict for result in run.results] == ["green", "green", "inconclusive"]
    assert "claude 1.2.0: inconclusive (attempt 1 of 2)" in seen


@pytest.mark.parametrize("verdict", ["red", "tooling"])
def test_a_red_or_tooling_release_is_never_retried(repo: Path, verdict: Verdict) -> None:
    _, asked, _ = _run(repo, ["1.0.0", "1.1.0", "1.2.0"], {"1.1.0": verdict})

    assert asked.count("1.1.0") == 1


@pytest.mark.parametrize("missing", ["1.0.0", "1.2.0"])
def test_a_list_lacking_a_pinned_bound_is_refused_whole(repo: Path, missing: str) -> None:
    releases = [item for item in ["1.0.0", "1.1.0", "1.2.0", "1.3.0"] if item != missing]
    qualify, asked = _qualifier({})

    with pytest.raises(DiscoveryError, match=missing.replace(".", r"\.")):
        run_harness(repo, _Adapter(releases), qualify=qualify, say=lambda _: None)  # type: ignore[arg-type]

    assert asked == []


def test_a_discovery_that_fails_stops_the_run(repo: Path) -> None:
    class Broken(_Adapter):
        def discover(self) -> list[str]:
            raise OSError("registry unreachable")

    qualify, asked = _qualifier({})

    with pytest.raises(OSError, match="registry unreachable"):
        run_harness(repo, Broken([]), qualify=qualify, say=lambda _: None)  # type: ignore[arg-type]

    assert asked == []


def test_discovery_keeps_only_releases_at_or_above_the_minimum() -> None:
    policy = VersionPolicy("1.0.0", "1.2.0")

    found = discover_releases(_Adapter(["0.1.0", "1.0.0", "1.2.0", "10.0.0"]), policy)  # type: ignore[arg-type]

    assert found == ["1.0.0", "1.2.0", "10.0.0"]


def _held(releases: list[str], results: list[Result], harness: str = "claude") -> Run:
    return Run(harness, VersionPolicy("1.0.0", "1.2.0"), tuple(releases), tuple(results))


def _outcomes(*versions: str, harness: str = "claude") -> list[Result]:
    return [Result(harness, version, "green") for version in versions]


def test_a_run_holds_exactly_one_outcome_per_release_in_order() -> None:
    run = _held(["1.0.0", "1.1.0", "1.2.0"], _outcomes("1.0.0", "1.1.0", "1.2.0"))

    assert [result.version for result in run.results] == list(run.releases)


@pytest.mark.parametrize(
    ("results", "needle"),
    [
        (_outcomes("1.0.0", "1.2.0"), "missing outcome for 1.1.0"),
        (_outcomes("1.0.0", "1.1.0", "1.1.0", "1.2.0"), "duplicate outcome for 1.1.0"),
        (_outcomes("1.0.0", "1.1.0", "1.2.0", "1.3.0"), "unexpected release 1.3.0"),
        (_outcomes("1.0.0", "1.1.0", "1.1.5"), "missing outcome for 1.2.0"),
        (_outcomes("1.0.0", "1.2.0", "1.1.0"), "out of order"),
        (
            [*_outcomes("1.0.0", "1.1.0"), Result("codex", "1.2.0", "green")],
            "outcome of codex among claude results",
        ),
        ([*_outcomes("1.0.0", "1.1.0"), Result("claude", "1.2.0", "passed")], "unknown verdict"),  # type: ignore[arg-type]
    ],
)
def test_missing_duplicate_or_wrong_outcomes_make_the_run_impossible(
    results: list[Result], needle: str
) -> None:
    with pytest.raises(IncompleteRun, match=needle):
        _held(["1.0.0", "1.1.0", "1.2.0"], results)


@pytest.mark.parametrize(
    "releases",
    [
        ["1.1.0", "1.2.0"],  # the pinned minimum was never tested
        ["1.0.0", "1.1.0"],  # the pinned maximum was never tested
        ["1.0.0", "1.2.0", "1.1.0"],
        ["1.0.0", "1.0.0", "1.2.0"],
    ],
)
def test_a_run_must_cover_both_pinned_bounds_in_ascending_unique_order(
    releases: list[str],
) -> None:
    with pytest.raises(IncompleteRun):
        _held(releases, _outcomes(*releases))


def test_a_qualifier_answering_for_another_release_stops_the_run_at_once(repo: Path) -> None:
    asked: list[str] = []

    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        asked.append(version)
        return Result(adapter.name, "1.1.0" if version == "1.0.0" else version, "green")

    with pytest.raises(IncompleteRun, match=r"returned the outcome of claude 1\.1\.0"):
        run_harness(
            repo, _Adapter(["1.0.0", "1.1.0", "1.2.0"]), qualify=qualify, say=lambda _: None
        )  # type: ignore[arg-type]

    assert asked == ["1.0.0"]


def test_a_qualifier_answering_for_another_harness_stops_the_run(repo: Path) -> None:
    def qualify(root: Path, adapter: Adapter, version: str) -> Result:
        return Result("codex", version, "green")

    with pytest.raises(IncompleteRun, match="outcome of codex"):
        run_harness(repo, _Adapter(["1.0.0", "1.2.0"]), qualify=qualify, say=lambda _: None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "found",
    [["1.0.0", "1.2.0", "1.1.0"], ["1.0.0", "1.0.0", "1.2.0"], ["1.0.0", "1.2.0", "1.3.0-rc.1"]],
)
def test_a_discovery_that_is_unordered_duplicated_or_unstable_is_refused(found: list[str]) -> None:
    with pytest.raises(DiscoveryError):
        discover_releases(_Adapter(found), VersionPolicy("1.0.0", "1.2.0"))  # type: ignore[arg-type]


def test_an_exclusion_missing_from_discovery_blocks_the_whole_run(repo: Path) -> None:
    (repo / "theater/harness/builtin/plugins/claude/compatibility.json").write_text(
        '{"min":"1.0.0","max":"1.2.0","excluded":["1.1.0"]}\n', encoding="utf-8"
    )

    qualify, asked = _qualifier({})
    with pytest.raises(DiscoveryError, match=r"1\.1\.0"):
        run_harness(repo, _Adapter(["1.0.0", "1.2.0"]), qualify=qualify, say=lambda _: None)
    assert asked == []
