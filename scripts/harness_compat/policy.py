"""Incremental policy updates and the guards for a one-line plugin JSON PR."""

from __future__ import annotations

import json
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


def derive(policy: VersionPolicy, results: tuple[Result, ...]) -> VersionPolicy:
    """Passes may advance max; every failure adds an exclusion without moving either bound."""
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
    return VersionPolicy(policy.minimum, maximum, tuple(sorted(excluded, key=version_key)))


def merge_pending(
    current: VersionPolicy, original: VersionPolicy, pending: VersionPolicy
) -> VersionPolicy:
    """Apply the pending PR's exclusion delta to main, preserving both maxima."""
    if current.minimum != original.minimum or current.minimum != pending.minimum:
        raise PolicyChangeError("the pinned minimum differs in the pending PR")
    removed = set(original.excluded) - set(pending.excluded)
    added = set(pending.excluded) - set(original.excluded)
    excluded = (set(current.excluded) - removed) | added
    return VersionPolicy(
        current.minimum,
        max(current.maximum, pending.maximum, key=version_key),
        tuple(sorted(excluded, key=version_key)),
    )


def check_change(old_text: str, new_text: str) -> None:
    """Refuse anything but a canonical one-line edit that keeps the pinned minimum."""
    try:
        old, new = VersionPolicy.from_text(old_text), VersionPolicy.from_text(new_text)
    except ValueError as exc:
        raise PolicyChangeError(f"the policy is not valid: {exc}") from exc
    if old_text != format_policy(old) or new_text != format_policy(new):
        raise PolicyChangeError("the policy is not the canonical single compact line")
    if new.minimum != old.minimum:
        raise PolicyChangeError(f"the pinned minimum moved: {old.minimum} to {new.minimum}")
    if version_key(new.maximum) < version_key(old.maximum):
        raise PolicyChangeError(f"the maximum shrank: {old.maximum} to {new.maximum}")


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
