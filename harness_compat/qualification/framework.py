"""Run the live suite in a subprocess and summarize its JUnit report."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from ..contract import Check, CheckStatus
from ..diagnostics import clean_diagnostic, clip_diagnostic
from .process import run_group

_DETAIL_LIMIT = 600


def _nodeid(case: ET.Element) -> str:
    parts = case.get("classname", "").split(".")
    index = next((i for i, part in enumerate(parts) if part.startswith("test_")), -1)
    if index < 0:
        return case.get("name", "")
    path = "/".join(parts[: index + 1]) + ".py"
    return "::".join([path, *parts[index + 1 :], case.get("name", "")])


def _case_status(case: ET.Element) -> str:
    if case.find("failure") is not None:
        return "failed"
    if case.find("error") is not None:
        return "errored"
    if case.find("skipped") is not None:
        return "skipped"
    return "passed"


def _failure_reason(case: ET.Element) -> str:
    event = case.find("failure")
    if event is None:
        event = case.find("error")
    if event is None:
        event = case.find("skipped")
    if event is None:
        return "no failure element"
    if message := clean_diagnostic(event.get("message", "")):
        return message
    captured = "\n".join(
        element.text or ""
        for tag in ("system-out", "system-err")
        if (element := case.find(tag)) is not None
    )
    return captured or event.text or "no captured failure output"


def _failure_detail(counts: str, nodeid: str, case: ET.Element) -> str:
    prefix = f"{counts}; {clip_diagnostic(nodeid, 200)}: "
    return prefix + clip_diagnostic(_failure_reason(case), _DETAIL_LIMIT - len(prefix), tail=True)


def _suite_check(target: str, cases: Sequence[ET.Element]) -> Check:
    if not cases:
        return Check(target, "error", "0 tests reported")
    counts = Counter(_case_status(case) for case in cases)
    detail = ", ".join(
        f"{counts[label]} {label}"
        for label in ("passed", "failed", "errored", "skipped")
        if counts[label]
    )
    if counts["failed"]:
        status: CheckStatus = "fail"
    elif counts["errored"] or counts["skipped"]:
        status = "error"
    else:
        status = "pass"
    if status in ("fail", "error"):
        first = next(
            (case for case in cases if _case_status(case) == "failed"),
            None,
        )
        if first is None:
            first = next(
                (case for case in cases if _case_status(case) != "passed"),
                None,
            )
        if first is not None:
            detail = _failure_detail(detail, _nodeid(first), first)
    return Check(target, status, clip_diagnostic(detail, _DETAIL_LIMIT))


def python_path(root: Path) -> str:
    """The code copy itself plus its workspace packages, so nothing resolves to another checkout."""
    sources = sorted((root / "packages").glob("*/src"))
    return os.pathsep.join(str(path) for path in (root, *sources))


def _environment(root: Path, env: Mapping[str, str]) -> dict[str, str]:
    return {**env, "PYTHONPATH": python_path(root), "PYTHONDONTWRITEBYTECODE": "1"}


def _refuse_checkout(root: Path) -> None:
    """Pytest writes bytecode and caches beside what it runs: never in a git checkout."""
    if (root / ".git").exists():
        raise ValueError(f"refusing to run pytest in the git checkout {root}; use a code copy")


def run_pytest(
    root: Path,
    target: str,
    *,
    env: Mapping[str, str],
    timeout: float = 900.0,
    pytest_args: Sequence[str] = (),
) -> Check:
    """Failures win; missing cases, errors and any skip authorize nothing.

    ``root`` is disposable; pytest leaves no bytecode or cache beside the code it runs.
    """
    _refuse_checkout(root)
    with tempfile.TemporaryDirectory(prefix="compat-pytest-") as temporary:
        xml = Path(temporary) / "junit.xml"
        argv = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "junit_logging=out-err",
            f"--junitxml={xml}",
            *pytest_args,
            target,
        ]
        try:
            run = run_group(argv, cwd=root, env=_environment(root, env), timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = clip_diagnostic(str(exc), _DETAIL_LIMIT, tail=True)
            return Check(target, "error", detail)
        if run.returncode not in (0, 1) or not xml.exists():
            detail = clip_diagnostic(run.stdout + "\n" + run.stderr, _DETAIL_LIMIT - 20, tail=True)
            return Check(target, "error", f"pytest exit {run.returncode}: {detail}")
        try:
            cases = list(ET.parse(xml).iter("testcase"))
        except ET.ParseError as exc:
            return Check(target, "error", f"invalid junit XML: {exc}")
        check = _suite_check(target, cases)
        if run.returncode != 0 and check.status == "pass":
            return Check(target, "error", f"pytest exit {run.returncode} despite passing cases")
        return check
