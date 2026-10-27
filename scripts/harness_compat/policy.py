"""Incremental policy updates and the guards for a one-line plugin JSON PR."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from theater.version_policy import VersionPolicy, version_key

from . import git
from .adapters import ADAPTER_NAMES
from .contract import Result

PLUGINS = Path("theater/harness/builtin/plugins")


class PolicyChangeError(ValueError):
    """A policy change that is not the routine, reviewable one-line edit."""


class Unproposable(ValueError):
    """Invalid qualification outcomes cannot change a policy."""


def policy_relative(name: str) -> Path:
    return PLUGINS / name / "compatibility.json"


def load_policy(root: Path, name: str) -> VersionPolicy:
    return VersionPolicy.load(root / policy_relative(name))


def format_policy(policy: VersionPolicy) -> str:
    """The canonical file text: compact, one line, newline-terminated."""
    return json.dumps(policy.to_json(), separators=(",", ":")) + "\n"


def write_policy(root: Path, name: str, policy: VersionPolicy) -> None:
    (root / policy_relative(name)).write_text(format_policy(policy), encoding="utf-8")


def admit_only(root: Path, name: str, version: str) -> None:
    """Admit just ``version`` in a disposable copy, so the gate alone never decides the result."""
    write_policy(root, name, VersionPolicy(version, version))
    if not load_policy(root, name).admits(version):
        raise RuntimeError(f"could not admit {name} {version} in the code copy")


def derive(
    policy: VersionPolicy,
    results: tuple[Result, ...],
    *,
    discovered: Sequence[str] = (),
    max_range: int | None = None,
) -> VersionPolicy:
    """Passes may advance max and raise the floor; failures only add exclusions."""
    verdicts = {result.version: result.verdict for result in results}
    if len(verdicts) != len(results):
        raise Unproposable("a release has more than one outcome")
    if any(version_key(version) < version_key(policy.minimum) for version in verdicts):
        raise Unproposable("an outcome is below the pinned minimum")
    maximum = policy.maximum
    excluded = set(policy.excluded)
    for version, outcome in verdicts.items():
        if outcome == "green":
            maximum = max(maximum, version, key=version_key)
            excluded.discard(version)
        elif outcome in {"red", "inconclusive", "tooling"}:
            excluded.add(version)
        else:
            raise Unproposable(f"unknown verdict {outcome!r} for {version}")
    proposal = VersionPolicy(policy.minimum, maximum, tuple(sorted(excluded, key=version_key)))
    if max_range is not None:
        proposal = raise_floor(proposal, discovered, max_range)
    return proposal


def floor_window(policy: VersionPolicy, discovered: Sequence[str]) -> tuple[str, ...]:
    """The discovered stable releases inside the policy range, ascending."""
    low, high = version_key(policy.minimum), version_key(policy.maximum)
    return tuple(version for version in discovered if low <= version_key(version) <= high)


def raise_floor(policy: VersionPolicy, discovered: Sequence[str], max_range: int) -> VersionPolicy:
    """Bound the range at the newest ``max_range`` discovered releases; the floor never falls."""
    if max_range < 1:
        raise ValueError("the floor range must hold at least one release")
    window = floor_window(policy, discovered)
    if len(window) <= max_range:
        return policy
    floor = version_key(window[-max_range])
    # The floor never sits on an excluded release; with none admitted in the top of the
    # window the policy stays unchanged rather than blocking max and exclusion publication.
    candidates = [
        version for version in window if version_key(version) >= floor and policy.admits(version)
    ]
    if not candidates:
        return policy
    excluded = tuple(
        version for version in policy.excluded if version_key(version) >= version_key(candidates[0])
    )
    return VersionPolicy(candidates[0], policy.maximum, excluded)


def merge_pending(
    current: VersionPolicy, original: VersionPolicy, pending: VersionPolicy
) -> VersionPolicy:
    """Apply the pending PR's exclusion delta to main, keeping main's floor; both maxima survive."""
    if current.minimum != original.minimum:
        raise PolicyChangeError("the pinned minimum moved on main since the pending PR")
    # A raised floor prunes exclusions below it; those are pruned, not removed.
    removed = {
        version
        for version in set(original.excluded) - set(pending.excluded)
        if version_key(version) >= version_key(pending.minimum)
    }
    added = set(pending.excluded) - set(original.excluded)
    excluded = (set(current.excluded) - removed) | added
    return VersionPolicy(
        current.minimum,
        max(current.maximum, pending.maximum, key=version_key),
        tuple(sorted(excluded, key=version_key)),
    )


def check_change(old_text: str, new_text: str) -> None:
    """Refuse anything but a canonical one-line edit that never lowers the floor."""
    try:
        old, new = VersionPolicy.from_text(old_text), VersionPolicy.from_text(new_text)
    except ValueError as exc:
        raise PolicyChangeError(f"the policy is not valid: {exc}") from exc
    if old_text != format_policy(old) or new_text != format_policy(new):
        raise PolicyChangeError("the policy is not the canonical single compact line")
    if version_key(new.minimum) < version_key(old.minimum):
        raise PolicyChangeError(f"the pinned minimum fell: {old.minimum} to {new.minimum}")
    if version_key(new.maximum) < version_key(old.maximum):
        raise PolicyChangeError(f"the maximum shrank: {old.maximum} to {new.maximum}")


def check_routine_floor(
    policy: VersionPolicy,
    base: VersionPolicy,
    discovered: Sequence[str],
    max_range: int,
) -> None:
    """A routine PR's floor is exactly the recomputed one: the newest ``max_range`` releases."""
    if max_range < 1:
        raise ValueError("the floor range must hold at least one release")
    # The floor is a pure function of (discovered, proposed max, max_range): the window
    # opens at main's floor, before the PR's own raise shrank it. Exclusions the PR
    # pruned below its floor still count as excluded when the floor is recomputed.
    pruned = {
        version for version in base.excluded if version_key(version) < version_key(policy.minimum)
    }
    pre_raise = VersionPolicy(
        base.minimum,
        policy.maximum,
        tuple(sorted(set(policy.excluded) | pruned, key=version_key)),
    )
    expected = raise_floor(pre_raise, discovered, max_range)
    if policy.minimum != expected.minimum:
        raise PolicyChangeError(
            f"the floor is not the newest {max_range} releases: "
            f"{policy.minimum}, expected {expected.minimum}"
        )


def changed_gates(root: Path, base: str) -> list[str]:
    """The harnesses whose policy file differs between ``base`` and HEAD."""
    changed = set(git.diff_names(root, base))
    return [name for name in ADAPTER_NAMES if policy_relative(name).as_posix() in changed]


def affected_harnesses(root: Path, base: str) -> list[str]:
    """Plugin changes select that harness; shared code, dependencies or suite changes select all."""
    changed = git.diff_names(root, base)
    shared_files = {"Makefile", "pyproject.toml", "uv.lock", "tests/conftest.py"}
    shared_prefixes = (
        "theater/",
        "packages/regie/src/",
        "scripts/harness_compat/",
        "tests/compat/",
        "tests/conformance/",
        "tests/support/",
        ".github/workflows/harness-compat",
    )
    if any(
        path in shared_files or path.startswith(shared_prefixes)
        for path in changed
        if not any(Path(path).is_relative_to(PLUGINS / name) for name in ADAPTER_NAMES)
    ):
        return list(ADAPTER_NAMES)
    return [
        name
        for name in ADAPTER_NAMES
        if any(Path(path).is_relative_to(PLUGINS / name) for path in changed)
    ]


def check_routine_change(root: Path, base: str) -> str:
    """A routine gate PR changes one policy file and nothing else; returns its harness."""
    changed = git.diff_names(root, base)
    gates = changed_gates(root, base)
    if len(gates) != 1 or len(changed) != 1:
        raise PolicyChangeError(
            f"a routine gate change touches exactly one compatibility.json, not {changed}"
        )
    relative = policy_relative(gates[0]).as_posix()
    new_text = (root / relative).read_text(encoding="utf-8")
    check_change(git.show(root, base, relative), new_text)
    return gates[0]
