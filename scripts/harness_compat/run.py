"""Qualify newer releases in order, publishing each finished outcome before the next."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from theater.version_policy import VersionPolicy, version_key

from .cleanup import CleanupFailed
from .contract import Adapter, Check, Result
from .diagnostics import clip_diagnostic, exception_detail
from .policy import load_policy
from .qualify import qualify_release

Qualifier = Callable[[Path, Adapter, str], Result]
_VERDICTS = {"green", "red", "inconclusive", "tooling"}
#: A release only starts with at least this much sweep time left.
_MIN_RELEASE_SECONDS = 600
#: This many identical tooling errors in a row end the batch: the rest cannot succeed either.
_TOOLING_STREAK = 3


class DiscoveryError(RuntimeError):
    """The upstream release list cannot be trusted."""


class IncompleteRun(RuntimeError):
    """An outcome does not identify the release requested."""


@dataclass(frozen=True, slots=True)
class Run:
    """Planned releases and their completed prefix, held only in memory."""

    harness: str
    policy: VersionPolicy
    releases: tuple[str, ...]
    results: tuple[Result, ...]
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        keys = [version_key(version) for version in self.releases]
        if keys != sorted(set(keys)):
            raise IncompleteRun(f"{self.harness} releases are not unique and ascending")
        if tuple(result.version for result in self.results) != self.releases[: len(self.results)]:
            raise IncompleteRun(f"{self.harness} outcomes are not the completed release prefix")
        if len(self.results) > len(self.releases):
            raise IncompleteRun(f"{self.harness} has unexpected outcomes")
        for result in self.results:
            _check_identity(result, self.harness, result.version)


def _check_identity(result: Result, harness: str, version: str) -> None:
    if (result.harness, result.version) != (harness, version) or result.verdict not in _VERDICTS:
        raise IncompleteRun(
            f"qualifying {harness} {version} returned {result.harness} {result.version} "
            f"with verdict {result.verdict!r}"
        )


def discover_releases(
    adapter: Adapter,
    policy: VersionPolicy,
    *,
    base: VersionPolicy | None = None,
    routine: bool = False,
) -> list[str]:
    """Sweeps start above committed max; PR checks stop at the proposed max."""
    found = adapter.discover()
    try:
        keys = [version_key(version) for version in found]
    except ValueError as exc:
        raise DiscoveryError(f"{adapter.name} discovery holds a non-stable release: {exc}") from exc
    if keys != sorted(set(keys)):
        raise DiscoveryError(f"{adapter.name} discovery is not unique and ascending")
    changed_exclusions = set(policy.excluded) ^ set(base.excluded) if base else set()
    for required in {policy.minimum, policy.maximum, *changed_exclusions}:
        if required not in found:
            raise DiscoveryError(f"{adapter.name} discovery lacks the policy release {required}")
    if base is None:
        return [version for version in found if version_key(version) > version_key(policy.maximum)]
    low = version_key(base.maximum) if routine else version_key(policy.minimum)
    high = version_key(policy.maximum)
    return [
        version
        for version in found
        if (low < version_key(version) <= high if routine else low <= version_key(version) <= high)
        or version in changed_exclusions
    ]


def split_shards(
    versions: Sequence[str], *, per_shard: int = 4, max_shards: int = 8
) -> list[list[str]]:
    """Split a PR verify window into bounded shards with explicit version lists."""
    if not versions:
        return []
    count = min(max_shards, -(-len(versions) // per_shard))
    size = -(-len(versions) // count)
    return [list(versions[start : start + size]) for start in range(0, len(versions), size)]


def _tooling_key(result: Result) -> tuple[str, str] | None:
    """The first failing check, normalized so one error matches across releases."""
    for check in result.checks:
        if check.status == "error":
            detail = " ".join(check.detail.replace(result.version, "<release>").split())
            return (check.name, detail)
    return None


def _budget_skip(deadline: float | None, index: int, total: int) -> str | None:
    """A release starts only with one release's minimum time left.

    Remaining time never grows, so once this trips the whole tail is skipped and
    skipped releases always sit above the highest green, hence above max.
    """
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    left = total - index
    if remaining < _MIN_RELEASE_SECONDS:
        return f"skipped: sweep time budget exhausted, {remaining:.0f}s left for {left} releases"
    return None


def _qualify_one(
    root: Path, adapter: Adapter, version: str, qualify: Qualifier
) -> tuple[Result, bool, str | None]:
    """One release outcome, a stop flag, and the error detail when the run must stop."""
    try:
        result = qualify(root, adapter, version)
        _check_identity(result, adapter.name, version)
    except (Exception, KeyboardInterrupt) as exc:
        detail = clip_diagnostic(exception_detail(exc), 500, tail=True)
        result = Result(
            adapter.name,
            version,
            "inconclusive" if isinstance(exc, KeyboardInterrupt) else "tooling",
            (*exc.checks, Check("cleanup", "error", detail))
            if isinstance(exc, CleanupFailed)
            else (Check("qualification", "error", detail),),
        )
        stop = isinstance(exc, (KeyboardInterrupt, CleanupFailed, IncompleteRun))
        return result, stop, detail if stop else None
    else:
        return result, False, None


def _advance_streak(
    result: Result, streak: int, streak_key: tuple[str, str] | None
) -> tuple[int, tuple[str, str] | None, str | None]:
    """Track identical consecutive tooling errors; the third arms skipping the rest."""
    if result.verdict != "tooling":
        return 0, None, None
    key = _tooling_key(result)
    if key is not None and key == streak_key:
        streak += 1
    else:
        streak = 1 if key is not None else 0
        streak_key = key
    if streak >= _TOOLING_STREAK and streak_key is not None:
        return (
            streak,
            streak_key,
            (f"skipped after {_TOOLING_STREAK} identical {streak_key[0]} tooling errors"),
        )
    return streak, streak_key, None


def run_harness(
    root: Path,
    adapter: Adapter,
    *,
    releases: Sequence[str] | None = None,
    qualify: Qualifier = qualify_release,
    publish: Callable[[Run], None] | None = None,
    say: Callable[[str], None] = lambda line: print(line, flush=True),
    budget_seconds: float | None = None,
) -> Run:
    """Continue after failed versions; an interruption or failed cleanup stops after publication."""
    policy = load_policy(root, adapter.name)
    chosen = tuple(discover_releases(adapter, policy) if releases is None else releases)
    Run(adapter.name, policy, chosen, ())
    results: list[Result] = []
    errors: list[str] = []
    # The outer job timeout stays the hard backstop; the budget decides which releases start.
    deadline = time.monotonic() + budget_seconds if budget_seconds is not None else None
    skip_detail: str | None = None
    streak = 0
    streak_key: tuple[str, str] | None = None
    for index, version in enumerate(chosen):
        say(f"compat:start {adapter.name} {version}")
        stop = False
        skip = skip_detail or _budget_skip(deadline, index, len(chosen))
        if skip is not None:
            result = Result(
                adapter.name, version, "inconclusive", (Check("skipped", "error", skip),)
            )
            streak, streak_key = 0, None
        else:
            result, stop, error = _qualify_one(root, adapter, version, qualify)
            if error is not None:
                errors.append(error)
            streak, streak_key, skip_detail = _advance_streak(result, streak, streak_key)
        results.append(result)
        say(f"compat:result {adapter.name} {version} {result.verdict}")
        for check in result.checks:
            say(f"{check.status:5} {check.name}: {check.detail}")
        if publish is not None:
            try:
                publish(Run(adapter.name, policy, chosen, tuple(results), tuple(errors)))
                say(f"compat:published {adapter.name} {version}")
            except (Exception, KeyboardInterrupt) as exc:
                detail = clip_diagnostic(
                    f"publication of {adapter.name} {version}: {exception_detail(exc)}",
                    500,
                    tail=True,
                )
                errors.append(detail)
                say(detail)
                results[-1] = replace(
                    result,
                    verdict=("inconclusive" if isinstance(exc, KeyboardInterrupt) else "tooling")
                    if result.verdict == "green"
                    else result.verdict,
                    checks=(*result.checks, Check("publication", "error", detail)),
                )
                say(f"compat:result {adapter.name} {version} {results[-1].verdict}")
                stop = stop or isinstance(exc, KeyboardInterrupt)
        if stop:
            break
    return Run(adapter.name, policy, chosen, tuple(results), tuple(errors))
