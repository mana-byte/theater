"""One harness, every published stable release from the pinned minimum to the latest."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from theater.harness.contracts.version_policy import VersionPolicy, version_key

from .contract import Adapter, Result
from .policy import load_policy
from .qualify import qualify_release

#: A release that ended inconclusive gets one more fresh attempt; red and tooling never do.
ATTEMPTS = 2

Qualifier = Callable[[Path, Adapter, str], Result]


def _say(text: str) -> None:
    """Progress straight to the log: a run lasts hours and CI buffers a piped stdout."""
    print(text, flush=True)


_VERDICTS = ("green", "red", "inconclusive", "tooling")


class DiscoveryError(RuntimeError):
    """The upstream release list cannot be trusted, so no release may be authorized from it."""


class IncompleteRun(RuntimeError):
    """The outcomes do not match the releases one for one, so nothing may be proposed from them."""


@dataclass(frozen=True, slots=True)
class Run:
    """Everything one run learned, held in memory until it is reported or published.

    ``releases`` is every release the run set out to test; ``results`` must hold exactly one
    outcome per release, in the same order, from this harness. Anything else cannot exist.
    """

    harness: str
    policy: VersionPolicy
    releases: tuple[str, ...]
    results: tuple[Result, ...]

    def __post_init__(self) -> None:
        keys = [version_key(version) for version in self.releases]
        if keys != sorted(set(keys)):
            raise IncompleteRun(f"{self.harness} releases are not unique and ascending")
        for bound in (self.policy.minimum, self.policy.maximum, *self.policy.excluded):
            if bound not in self.releases:
                raise IncompleteRun(f"{self.harness} run never covered the pinned release {bound}")
        if self.releases[0] != self.policy.minimum:
            raise IncompleteRun(f"{self.harness} run starts above the pinned minimum")
        versions = [result.version for result in self.results]
        problems = [
            *(f"missing outcome for {v}" for v in self.releases if v not in versions),
            *(f"outcome for unexpected release {v}" for v in versions if v not in self.releases),
            *(f"duplicate outcome for {v}" for v, n in Counter(versions).items() if n > 1),
            *(
                f"outcome of {r.harness} among {self.harness} results"
                for r in self.results
                if r.harness != self.harness
            ),
            *(
                f"unknown verdict {r.verdict!r} for {r.version}"
                for r in self.results
                if r.verdict not in _VERDICTS
            ),
        ]
        if not problems and versions != list(self.releases):
            problems.append("outcomes are out of order")
        if problems:
            raise IncompleteRun(f"{self.harness} run is incomplete: {'; '.join(problems)}")


def discover_releases(adapter: Adapter, policy: VersionPolicy) -> list[str]:
    """Every stable release from ``policy.minimum`` up, ascending: admitted, excluded and new.

    A list that lacks the pinned bounds is incomplete or wrong, and is refused whole.
    """
    found = adapter.discover()
    try:
        keys = [version_key(version) for version in found]
    except ValueError as exc:
        raise DiscoveryError(f"{adapter.name} discovery holds a non-stable release: {exc}") from exc
    if keys != sorted(set(keys)):
        raise DiscoveryError(f"{adapter.name} discovery is not unique and ascending")
    for bound in (policy.minimum, policy.maximum, *policy.excluded):
        if bound not in found:
            raise DiscoveryError(f"{adapter.name} discovery lacks the pinned release {bound}")
    low = version_key(policy.minimum)
    return [version for version in found if version_key(version) >= low]


def run_harness(
    root: Path,
    adapter: Adapter,
    *,
    qualify: Qualifier = qualify_release,
    say: Callable[[str], None] = _say,
) -> Run:
    """Qualify every discovered release, ascending and sequentially, continuing past failures."""
    policy = load_policy(root, adapter.name)
    releases = discover_releases(adapter, policy)
    say(f"{adapter.name}: {len(releases)} releases from {policy.minimum} ({policy.range_text})")
    results: list[Result] = []
    for version in releases:
        for attempt in range(1, ATTEMPTS + 1):
            result = qualify(root, adapter, version)
            if result.verdict != "inconclusive":
                break
            say(f"{adapter.name} {version}: inconclusive (attempt {attempt} of {ATTEMPTS})")
        if (result.harness, result.version) != (adapter.name, version):
            raise IncompleteRun(
                f"qualifying {adapter.name} {version} returned the outcome of "
                f"{result.harness} {result.version}"
            )
        results.append(result)
        say(f"{adapter.name} {version}: {result.verdict}")
    return Run(adapter.name, policy, tuple(releases), tuple(results))
