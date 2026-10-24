"""Run the live conformance suite for one harness release and reconcile what it proved.

The suite runs in a pytest subprocess inside the pipeline sandbox; this module never imports
``theater``. It reads the two documents the suite writes to the evidence directory:
``required.json`` (everything the manifest promises, written before any test runs) and
``inventory.json`` (what each obligation did), and turns them into one check per obligation.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import (
    SUITE_DOCUMENT_SCHEMA,
    SUITE_ENV_BINARY,
    SUITE_ENV_EVIDENCE_DIR,
    SUITE_ENV_HARNESS,
    SUITE_ENV_SANDBOX_ROOT,
    SUITE_ENV_VERSION,
    Check,
    CheckStatus,
    Sandbox,
)
from .diagnostics import clip_diagnostic
from .framework import run_pytest

#: The pytest target of the live suite; it is collected only under the pipeline sandbox token.
SUITE_TARGET = "tests/conformance/live"
#: One attempt may not exceed the runner's attempt cap.
SUITE_TIMEOUT_SECONDS = 20 * 60.0
_DETAIL_LIMIT = 300
_OUTCOMES: Mapping[str, CheckStatus] = {"passed": "pass", "failed": "fail"}
_NEVER_RAN = "its scenario never ran"
_VERSION = re.compile(r"\d+\.\d+\.\d+")


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """One check per obligation, plus the suite-level check and any reconciliation problems."""

    checks: tuple[Check, ...]
    #: Reconciliation failures: our machinery lost an obligation, not the CLI misbehaving.
    problems: tuple[str, ...] = ()


def installed_version(binary: Path) -> str:
    """The MAJOR.MINOR.PATCH an installed CLI reports for ``--version``."""
    shown = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
    ).stdout
    found = _VERSION.search(shown)
    if found is None:
        raise ValueError(f"cannot read a version from {binary} --version: {shown.strip()!r}")
    return found.group(0)


def suite_environment(sb: Sandbox, *, harness: str, binary: Path, version: str) -> dict[str, str]:
    """The environment of the suite's pytest process: hermetic, offline except the loopback mock."""
    theater_home = sb.root / "theater"
    theater_home.mkdir(parents=True, exist_ok=True)
    return {
        **sb.offline_env,
        SUITE_ENV_HARNESS: harness,
        SUITE_ENV_BINARY: str(binary),
        SUITE_ENV_VERSION: version,
        SUITE_ENV_SANDBOX_ROOT: str(sb.root),
        SUITE_ENV_EVIDENCE_DIR: str(sb.evidence_dir),
        "THEATER_HOME": str(theater_home),
    }


def run_suite(
    root: Path,
    sb: Sandbox,
    *,
    harness: str,
    binary: Path,
    version: str,
    pytest_args: Sequence[str] = (),
    timeout: float = SUITE_TIMEOUT_SECONDS,
    extra_env: Mapping[str, str] | None = None,
) -> SuiteReport:
    """Run every live scenario the harness's manifest derives; partial runs relax reconciliation."""
    env = {**suite_environment(sb, harness=harness, binary=binary, version=version)}
    env.update(extra_env or {})
    (suite,) = run_pytest(
        root,
        (SUITE_TARGET,),
        env=env,
        expected_skips={},
        timeout=timeout,
        pytest_args=pytest_args,
    )
    return reconcile(
        suite,
        _document(sb.evidence_dir / "required.json"),
        _document(sb.evidence_dir / "inventory.json"),
        harness=harness,
        version=version,
        partial=bool(pytest_args),
    )


def _document(path: Path) -> Mapping[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def reconcile(
    suite: Check,
    required: Mapping[str, Any] | None,
    inventory: Mapping[str, Any] | None,
    *,
    harness: str,
    version: str,
    partial: bool = False,
) -> SuiteReport:
    """Turn the suite's own accounting into checks; a promised obligation never vanishes quietly."""
    problems = _document_problems(required, inventory, harness, version)
    if problems:
        return SuiteReport((suite, *_fault(problems)), tuple(problems))
    assert required is not None and inventory is not None
    results = {entry["id"]: entry for entry in inventory["obligations"]}
    checks: list[Check] = [suite]
    for entry in required["obligations"]:
        found = results.get(entry["id"])
        if found is None:
            problems.append(f"obligation {entry['id']} is missing from inventory.json")
            continue
        check = _obligation_check(entry, found, partial=partial)
        if check.detail == _NEVER_RAN:
            problems.append(f"obligation {entry['id']} has a scenario that never ran")
        checks.append(check)
    if not partial:
        for extra in sorted(set(results) - {entry["id"] for entry in required["obligations"]}):
            problems.append(f"inventory.json holds {extra}, which required.json never promised")
    checks.extend(_fault(problems))
    return SuiteReport(tuple(checks), tuple(problems))


def _document_problems(
    required: Mapping[str, Any] | None,
    inventory: Mapping[str, Any] | None,
    harness: str,
    version: str,
) -> list[str]:
    problems: list[str] = []
    for label, document in (("required.json", required), ("inventory.json", inventory)):
        if document is None:
            problems.append(f"{label} is missing or unreadable: the suite did not run to the end")
        elif document.get("schema") != SUITE_DOCUMENT_SCHEMA:
            problems.append(f"{label} has schema {document.get('schema')!r}")
        elif (document.get("harness"), document.get("version")) != (harness, version):
            problems.append(
                f"{label} describes {document.get('harness')} {document.get('version')}"
            )
    return problems


def _obligation_check(
    entry: Mapping[str, Any], found: Mapping[str, Any], *, partial: bool
) -> Check:
    name = f"conformance:{entry['id']}"
    detail = clip_diagnostic(str(found.get("detail") or ""), _DETAIL_LIMIT)
    if found.get("status") != "executed":
        if entry.get("scenario") is None:
            return Check(name, "skip", "no live scenario yet")
        if partial:
            return Check(name, "skip", "not selected in this partial run")
        return Check(name, "error", _NEVER_RAN)
    outcome = str(found.get("outcome"))
    return Check(name, _OUTCOMES.get(outcome, "error"), detail or outcome)


def _fault(problems: Sequence[str]) -> tuple[Check, ...]:
    return tuple(Check("conformance:reconcile", "error", problem) for problem in problems)
