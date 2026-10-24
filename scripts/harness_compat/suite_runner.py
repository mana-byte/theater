"""Run the live conformance suite for one harness release and reconcile what it proved.

The suite runs in a pytest subprocess inside the pipeline sandbox. This module reads the two
documents the suite writes into the sandbox's transient evidence directory: ``required.json``
(everything the manifest promises, written before any test runs) and ``inventory.json`` (what
each obligation did), and turns them into one check per obligation. Nothing here outlives the
sandbox.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from theater.harness.contracts.version_policy import version_key

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
    Verdict,
)
from .diagnostics import clip_diagnostic
from .framework import run_pytest
from .process import run_group
from .sandbox import make_sandbox

#: The pytest target of the live suite; it is collected only under the pipeline sandbox token.
SUITE_TARGET = "tests/conformance/live"
#: The live scenarios of one release may not run longer than this.
SUITE_TIMEOUT_SECONDS = 20 * 60.0
_DETAIL_LIMIT = 300
_OUTCOMES: Mapping[str, CheckStatus] = {"passed": "pass", "failed": "fail"}
#: A word that opens like a release (MAJOR.MINOR.PATCH...): it must then be exactly one.
_VERSION_WORD = re.compile(r"v?(\d+\.\d+\.\d+\S*)")
_WORD_BREAKS = re.compile(r"[\s()\[\],;:\"']+")


@dataclass(frozen=True, slots=True)
class SuiteReport:
    """One check per obligation, plus the suite-level check and any reconciliation problems."""

    checks: tuple[Check, ...]
    #: Reconciliation failures: our machinery lost an obligation, not the CLI misbehaving.
    problems: tuple[str, ...] = ()
    #: Pytest arguments selected part of the suite: nothing proven here can authorize a release.
    partial: bool = False


def _reported_versions(text: str) -> set[str]:
    """The releases ``text`` names; a word shaped like one but not canonical stable raises."""
    found: set[str] = set()
    for word in _WORD_BREAKS.split(text):
        if (match := _VERSION_WORD.fullmatch(word)) is None:
            continue
        try:
            version_key(candidate := match.group(1))
        except ValueError as exc:
            raise ValueError(f"{word!r} is not a canonical stable release") from exc
        found.add(candidate)
    return found


def installed_version(binary: Path) -> str:
    """The one MAJOR.MINOR.PATCH an installed CLI reports for ``--version``.

    The CLI starts offline in a throwaway home that is also its working directory, so it never
    sees the real home or the checkout. A failed exit, no release, a pre-release or build suffix,
    or two different releases are all refused. Stdout wins; stderr is read only when stdout names
    no release (some CLIs print it there).
    """
    binary = binary.resolve()
    with make_sandbox("version-probe") as sb:
        shown = run_group(
            [str(binary), "--version"],
            cwd=sb.home,
            env=sb.offline_env,
            timeout=30,
        )
    if shown.returncode != 0:
        detail = clip_diagnostic(shown.stdout + shown.stderr, 200, tail=True)
        raise ValueError(f"{binary} --version exited {shown.returncode}: {detail}")
    for text in (shown.stdout, shown.stderr):
        if versions := _reported_versions(text):
            if len(versions) > 1:
                raise ValueError(f"{binary} --version names {sorted(versions)}: ambiguous")
            return versions.pop()
    raise ValueError(
        f"cannot read a version from {binary} --version: {(shown.stdout + shown.stderr).strip()!r}"
    )


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
    suite = run_pytest(
        root,
        SUITE_TARGET,
        env=env,
        timeout=timeout,
        pytest_args=pytest_args,
    )
    return reconcile(
        suite,
        _load(sb.evidence_dir / "required.json", inventory=False),
        _load(sb.evidence_dir / "inventory.json", inventory=True),
        harness=harness,
        version=version,
        partial=bool(pytest_args or extra_env),
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
        return SuiteReport((suite, *_fault(problems)), tuple(problems), partial)
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
    return SuiteReport(tuple(checks), tuple(problems), partial)


def _obligation_check(promised: SuiteRow, found: SuiteRow, *, partial: bool) -> tuple[Check, bool]:
    """The obligation's check, and whether its scenario was owed but never ran."""
    name = f"conformance:{promised.id}"
    if not found.executed:
        if partial:
            return Check(name, "skip", "not selected in this partial run"), False
        return Check(name, "error", "its scenario never ran"), True
    detail = clip_diagnostic(found.detail, _DETAIL_LIMIT)
    assert found.outcome is not None
    return Check(name, _OUTCOMES.get(found.outcome, "error"), detail or found.outcome), False


def _fault(problems: Sequence[str]) -> tuple[Check, ...]:
    return tuple(Check("conformance:reconcile", "error", problem) for problem in problems)


def verdict(report: SuiteReport) -> Verdict:
    """The suite is the sole authority, and only a whole, clean pass is green.

    A failed obligation is red. Reconciliation problems beside a clean pytest run are our own
    accounting (tooling). Anything unproven (an error, a skipped or unrun obligation, a filtered
    run, or no passing obligation at all) says nothing about the release, so it is inconclusive.
    """
    checks = report.checks
    if any(check.status == "fail" for check in checks):
        return "red"
    if report.problems and checks[0].status == "pass":
        return "tooling"
    if report.partial or any(check.status in ("error", "skip") for check in checks):
        return "inconclusive"
    proven = any(check.name.startswith("conformance:") for check in checks)
    return "green" if proven and checks[0].status == "pass" else "inconclusive"
