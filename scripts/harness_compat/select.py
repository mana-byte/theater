"""Pure release selection and promotion policy."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .contract import Adapter, Result, VerifiedSet, is_stable, version_key


def _exact_floor(gate: VerifiedSet) -> str | None:
    return min(gate.versions, key=version_key) if gate.versions else None


def select_candidates(
    adapter: Adapter,
    gate: VerifiedSet,
    evidenced: Iterable[str] = (),
    *,
    max_candidates: int = 5,
    version: str | None = None,
    releases: Iterable[str] | None = None,
) -> list[str]:
    """Choose stable unproved releases; explicit versions override evidence.
    Exact gates choose one newest release above the oldest admission.
    """
    if version is not None:
        if not is_stable(version):
            raise ValueError(f"not a stable release: {version}")
        return [version]
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    seen = set(evidenced)
    found = adapter.discover() if releases is None else releases
    pending = sorted(
        {item for item in found if is_stable(item)} - seen - gate.known_bad, key=version_key
    )
    if gate.kind == "exact":
        floor = _exact_floor(gate)
        eligible = [
            item
            for item in pending
            if not gate.admits(item) and (floor is None or version_key(item) > version_key(floor))
        ]
        return eligible[-1:]
    if gate.ceiling is None:
        return []
    return [item for item in pending if version_key(item) >= version_key(gate.ceiling)][
        :max_candidates
    ]


def newest_in_range(
    adapter: Adapter, gate: VerifiedSet, releases: Iterable[str] | None = None
) -> str | None:
    """Watch just the newest stable release currently admitted by a range gate."""
    if gate.kind != "range":
        return None
    found = adapter.discover() if releases is None else releases
    admitted = [item for item in found if is_stable(item) and gate.admits(item)]
    return max(admitted, key=version_key) if admitted else None


def plan_bump(gate: VerifiedSet, results: Sequence[Result], releases: Iterable[str]) -> list[str]:
    """Promote the newest exact green, or a range's unbroken run of releases.
    A range never skips a release without a green result, whatever the candidate cap or forcing.
    """
    greens = {result.version for result in results if result.verdict == "green"}
    if gate.kind == "exact":
        floor = _exact_floor(gate)
        eligible = {
            version
            for version in greens - gate.known_bad
            if not gate.admits(version)
            and (floor is None or version_key(version) > version_key(floor))
        }
        return [max(eligible, key=version_key)] if eligible else []
    if gate.ceiling is None:  # nothing to move: greens are evidence only
        planned: list[str] = []
        for result in sorted(results, key=lambda item: version_key(item.version)):
            if result.version in gate.known_bad or result.verdict != "green":
                break
            planned.append(result.version)
        return planned
    planned = []
    for version in sorted({item for item in releases if is_stable(item)}, key=version_key):
        if version_key(version) < version_key(gate.ceiling) or version in gate.known_bad:
            continue
        if version not in greens:
            break
        planned.append(version)
    return planned
