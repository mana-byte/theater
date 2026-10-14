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
) -> list[str]:
    """Choose stable unproved releases; explicit versions override evidence.
    Automatic exact selection ignores releases no newer than the oldest admission.
    """
    if version is not None:
        if not is_stable(version):
            raise ValueError(f"not a stable release: {version}")
        return [version]
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    seen = set(evidenced)
    releases = sorted(
        {item for item in adapter.discover() if is_stable(item)} - seen - gate.known_bad,
        key=version_key,
    )
    if gate.kind == "exact":
        floor = _exact_floor(gate)
        return [
            item
            for item in releases
            if not gate.admits(item) and (floor is None or version_key(item) > version_key(floor))
        ][-max_candidates:]
    if gate.ceiling is None:
        return []
    return [item for item in releases if version_key(item) >= version_key(gate.ceiling)][
        :max_candidates
    ]


def newest_in_range(adapter: Adapter, gate: VerifiedSet) -> str | None:
    """Watch just the newest stable release currently admitted by a range gate."""
    if gate.kind != "range":
        return None
    admitted = [item for item in adapter.discover() if is_stable(item) and gate.admits(item)]
    return max(admitted, key=version_key) if admitted else None


def plan_bump(gate: VerifiedSet, results: Sequence[Result]) -> list[str]:
    """Promote greens above an exact gate's floor or through a range's green prefix."""
    if gate.kind == "exact":
        floor = _exact_floor(gate)
        return sorted(
            {
                result.version
                for result in results
                if result.verdict == "green"
                and result.version not in gate.known_bad
                and (floor is None or version_key(result.version) > version_key(floor))
            },
            key=version_key,
        )
    greens: list[str] = []
    for result in sorted(results, key=lambda item: version_key(item.version)):
        if result.version in gate.known_bad or result.verdict != "green":
            break
        greens.append(result.version)
    return greens
