"""Qualify newer releases in order, publishing each finished outcome before the next."""

from __future__ import annotations

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


def run_harness(
    root: Path,
    adapter: Adapter,
    *,
    releases: Sequence[str] | None = None,
    qualify: Qualifier = qualify_release,
    publish: Callable[[Run], None] | None = None,
    say: Callable[[str], None] = lambda line: print(line, flush=True),
) -> Run:
    """Continue after failed versions; an interruption or failed cleanup stops after publication."""
    policy = load_policy(root, adapter.name)
    chosen = tuple(discover_releases(adapter, policy) if releases is None else releases)
    Run(adapter.name, policy, chosen, ())
    results: list[Result] = []
    errors: list[str] = []
    for version in chosen:
        say(f"compat:start {adapter.name} {version}")
        stop = False
        try:
            result = qualify(root, adapter, version)
            _check_identity(result, adapter.name, version)
        except (Exception, KeyboardInterrupt) as exc:
            detail = clip_diagnostic(exception_detail(exc), 500, tail=True)
            interrupted = isinstance(exc, KeyboardInterrupt)
            result = Result(
                adapter.name,
                version,
                "inconclusive" if interrupted else "tooling",
                (Check("qualification", "error", detail),),
            )
            stop = isinstance(exc, (KeyboardInterrupt, CleanupFailed, IncompleteRun))
            if stop:
                errors.append(detail)
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
