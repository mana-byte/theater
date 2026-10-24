"""The version policy a run proposes, and the checks that keep a proposal honest.

The policy is one compact line of JSON per plugin, read by the plugin at runtime. Evidence decides
it: a release is admitted only when the live suite passed it, and every other release beneath the
newest pass is excluded by name. The minimum is pinned by people and never moves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from theater.harness.contracts.version_policy import VersionPolicy, version_key

from . import git
from .adapters import ADAPTER_NAMES
from .contract import Result

PLUGINS = Path("theater/harness/builtin/plugins")


class PolicyChangeError(ValueError):
    """A policy change that is not the routine, reviewable one-line edit."""


class Unproposable(ValueError):
    """The evidence cannot support any policy; the message says why."""


@dataclass(frozen=True, slots=True)
class Mismatch:
    """A release whose current outcome disagrees with what the policy says about it."""

    version: str
    kind: Literal["regression", "stale-exclusion"]
    verdict: str


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


def mismatches(policy: VersionPolicy, results: tuple[Result, ...]) -> list[Mismatch]:
    """Releases inside the policy's bounds whose admission and verdict disagree."""
    low, high = version_key(policy.minimum), version_key(policy.maximum)
    found: list[Mismatch] = []
    for result in sorted(results, key=lambda item: version_key(item.version)):
        if not low <= version_key(result.version) <= high:
            continue
        green = result.verdict == "green"
        if policy.admits(result.version) and not green:
            found.append(Mismatch(result.version, "regression", result.verdict))
        elif not policy.admits(result.version) and green:
            found.append(Mismatch(result.version, "stale-exclusion", result.verdict))
    return found


def derive(policy: VersionPolicy, results: tuple[Result, ...]) -> VersionPolicy:
    """The newest pass is the maximum; every other tested release below it is excluded."""
    verdicts = {result.version: result.verdict for result in results}
    if len(verdicts) != len(results):
        raise Unproposable("a release has more than one outcome")
    required = {policy.minimum, policy.maximum, *policy.excluded}
    if missing := required - verdicts.keys():
        raise Unproposable(f"no outcome for policy releases {sorted(missing, key=version_key)}")
    if any(version_key(version) < version_key(policy.minimum) for version in verdicts):
        raise Unproposable("an outcome is below the pinned minimum")
    greens = [version for version, verdict in verdicts.items() if verdict == "green"]
    if not greens:
        raise Unproposable("no release passed the full suite")
    maximum = max(greens, key=version_key)
    excluded = sorted(
        (version for version, verdict in verdicts.items() if verdict != "green"),
        key=version_key,
    )
    return VersionPolicy(
        policy.minimum,
        maximum,
        tuple(
            v
            for v in excluded
            if version_key(policy.minimum) <= version_key(v) < version_key(maximum)
        ),
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


def check_working_tree(root: Path, name: str) -> None:
    """The proposal is exactly one file with one line replaced; nothing else is dirty."""
    relative = policy_relative(name).as_posix()
    if (touched := git.changed_paths(root)) != {relative}:
        raise PolicyChangeError(f"the change touches {sorted(touched)}, not only {relative}")
    if (counts := git.numstat(root, relative)) != (1, 1):
        raise PolicyChangeError(f"the change is +{counts[0]}/-{counts[1]} lines, not +1/-1")


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
        if not Path(path).is_relative_to(PLUGINS)
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
