"""Run the live conformance suite for one harness release and reconcile what it proved.

The suite runs in a pytest subprocess inside the pipeline sandbox; this module never imports
``theater``. It reads the two documents the suite writes to the evidence directory:
``required.json`` (everything the manifest promises, written before any test runs) and
``inventory.json`` (what each obligation did), and turns them into one check per obligation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .contract import (
    SUITE_ENV_BINARY,
    SUITE_ENV_EVIDENCE_DIR,
    SUITE_ENV_HARNESS,
    SUITE_ENV_SANDBOX_ROOT,
    SUITE_ENV_VERSION,
    Check,
    CheckStatus,
    Sandbox,
    SuiteDocument,
    SuiteDocumentError,
    SuiteRow,
)
from .diagnostics import clip_diagnostic
from .framework import run_pytest

#: The pytest target of the live suite; it is collected only under the pipeline sandbox token.
SUITE_TARGET = "tests/conformance/live"
#: One attempt may not exceed the runner's attempt cap.
SUITE_TIMEOUT_SECONDS = 20 * 60.0
_DETAIL_LIMIT = 300
_OUTCOMES: Mapping[str, CheckStatus] = {"passed": "pass", "failed": "fail"}
#: Where the workflow collects diagnostics to upload as an artifact (Codex's collector uses it too).
DIAGNOSTICS_ENV = "THEATER_COMPAT_DIAGNOSTICS"
_KEPT_FILE_BYTES = 1_000_000
_KEPT_TOTAL_BYTES = 20_000_000
_VERSION = re.compile(r"\d+\.\d+\.\d+")


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """One check per obligation, plus the suite-level check and any reconciliation problems."""

    checks: tuple[Check, ...]
    #: Reconciliation failures: our machinery lost an obligation, not the CLI misbehaving.
    problems: tuple[str, ...] = ()
    #: Every obligation id the manifest promised, for the cross-run baseline.
    inventory: tuple[str, ...] = ()


def installed_version(binary: Path) -> str:
    """The MAJOR.MINOR.PATCH an installed CLI reports for ``--version``, from a throwaway home.

    A CLI may log or write config on any start, so it never sees the real home.
    """
    with tempfile.TemporaryDirectory(prefix="v") as home:
        env = {
            "HOME": home,
            "PATH": os.environ.get("PATH", ""),
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_CACHE_HOME": f"{home}/.cache",
            "XDG_DATA_HOME": f"{home}/.local/share",
            "XDG_STATE_HOME": f"{home}/.local/state",
        }
        shown = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30, check=False, env=env
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
        _load(sb.evidence_dir / "required.json", inventory=False),
        _load(sb.evidence_dir / "inventory.json", inventory=True),
        harness=harness,
        version=version,
        partial=bool(pytest_args),
    )


def _load(path: Path, *, inventory: bool) -> SuiteDocument | str:
    """The parsed document, or why it cannot be trusted."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return f"{path.name} is missing or unreadable: the suite did not run to the end"
    try:
        return SuiteDocument.parse(body, inventory=inventory)
    except SuiteDocumentError as exc:
        return f"{path.name} {exc}"


def reconcile(
    suite: Check,
    required: SuiteDocument | str,
    inventory: SuiteDocument | str,
    *,
    harness: str,
    version: str,
    partial: bool = False,
) -> SuiteReport:
    """Turn the suite's own accounting into checks; a promised obligation never vanishes quietly.

    A document that could not be loaded arrives as the reason it cannot be trusted.
    """
    problems = [document for document in (required, inventory) if isinstance(document, str)]
    for label, document in (("required.json", required), ("inventory.json", inventory)):
        if isinstance(document, SuiteDocument) and (document.harness, document.version) != (
            harness,
            version,
        ):
            problems.append(f"{label} describes {document.harness} {document.version}")
    if problems:
        return SuiteReport((suite, *_fault(problems)), tuple(problems))
    assert isinstance(required, SuiteDocument) and isinstance(inventory, SuiteDocument)
    results = {row.id: row for row in inventory.rows}
    checks: list[Check] = [suite]
    for promised in required.rows:
        found = results.get(promised.id)
        if found is None:
            problems.append(f"obligation {promised.id} is missing from inventory.json")
            continue
        check, never_ran = _obligation_check(promised, found, partial=partial)
        if never_ran:
            problems.append(f"obligation {promised.id} has a scenario that never ran")
        checks.append(check)
    if not partial:
        promised_ids = {row.id for row in required.rows}
        for extra in sorted(set(results) - promised_ids):
            problems.append(f"inventory.json holds {extra}, which required.json never promised")
    checks.extend(_fault(problems))
    return SuiteReport(tuple(checks), tuple(problems), tuple(row.id for row in required.rows))


def _obligation_check(promised: SuiteRow, found: SuiteRow, *, partial: bool) -> tuple[Check, bool]:
    """The obligation's check, and whether its scenario was owed but never ran."""
    name = f"conformance:{promised.id}"
    if not found.executed:
        if promised.scenario is None:
            return Check(name, "skip", "no live scenario yet"), False
        if partial:
            return Check(name, "skip", "not selected in this partial run"), False
        return Check(name, "error", "its scenario never ran"), True
    detail = clip_diagnostic(found.detail, _DETAIL_LIMIT)
    assert found.outcome is not None
    return Check(name, _OUTCOMES.get(found.outcome, "error"), detail or found.outcome), False


def _fault(problems: Sequence[str]) -> tuple[Check, ...]:
    return tuple(Check("conformance:reconcile", "error", problem) for problem in problems)


def shadow_checks(
    root: Path, sb: Sandbox, *, harness: str, binary: Path, version: str
) -> tuple[Check, ...]:
    """The suite's result as a second opinion: whatever goes wrong here never touches a verdict."""
    try:
        report = run_suite(root, sb, harness=harness, binary=binary, version=version)
        if any(check.status in ("fail", "error") for check in report.checks):
            _keep_diagnostics(sb.evidence_dir, f"{harness}-{version}-suite")
    except Exception as exc:
        return (Check("shadow", "error", clip_diagnostic(f"{type(exc).__name__}: {exc}", 300)),)
    return report.checks


def _keep_diagnostics(evidence: Path, name: str) -> None:
    """Copy a failing run's logs, pane captures and request records where the workflow uploads."""
    target = os.environ.get(DIAGNOSTICS_ENV)
    if not target:
        return
    kept = 0
    for source in sorted(evidence.rglob("*")):
        if not source.is_file() or source.stat().st_size > _KEPT_FILE_BYTES:
            continue
        kept += source.stat().st_size
        if kept > _KEPT_TOTAL_BYTES:
            break
        destination = Path(target) / name / source.relative_to(evidence)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
