"""Run pytest in a subprocess and aggregate its JUnit report per selector."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from .contract import Check, CheckStatus
from .diagnostics import clean_diagnostic, clip_diagnostic
from .process import run_group

_DETAIL_LIMIT = 600


def _nodeid(case: ET.Element) -> str:
    parts = case.get("classname", "").split(".")
    index = next((i for i, part in enumerate(parts) if part.startswith("test_")), -1)
    if index < 0:
        return case.get("name", "")
    path = "/".join(parts[: index + 1]) + ".py"
    return "::".join([path, *parts[index + 1 :], case.get("name", "")])


def _selects(selector: str, nodeid: str) -> bool:
    return nodeid == selector or any(
        nodeid.startswith(selector + boundary) for boundary in ("/", "::", "[")
    )


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


def _check_selector(
    selector: str, cases: Mapping[str, ET.Element], expected_skips: Mapping[str, str]
) -> Check:
    selected = [(nodeid, case) for nodeid, case in cases.items() if _selects(selector, nodeid)]
    if not selected:
        return Check(selector, "error", "0 matched; pytest did not report this selector")
    counts: Counter[str] = Counter()
    unexpected_skips: list[str] = []
    for nodeid, case in selected:
        kind = _case_status(case)
        counts[kind] += 1
        if kind == "skipped" and not any(_selects(key, nodeid) for key in expected_skips):
            unexpected_skips.append(nodeid)
    detail = ", ".join(
        f"{counts[label]} {label}"
        for label in ("passed", "failed", "errored", "skipped")
        if counts[label]
    )
    if counts["failed"]:
        status: CheckStatus = "fail"
    elif counts["errored"] or unexpected_skips:
        status = "error"
    elif counts["passed"] == len(selected):
        status = "pass"
    elif counts["skipped"] == len(selected) and selector in expected_skips:
        status = "skip"
    else:
        status = "error"
        detail += "; selector did not fully pass or skip"
    if status in ("fail", "error"):
        first = next(
            ((nodeid, case) for nodeid, case in selected if _case_status(case) == "failed"),
            None,
        )
        if first is None:
            first = next(
                ((nodeid, case) for nodeid, case in selected if _case_status(case) == "errored"),
                None,
            )
        if first is not None:
            detail = _failure_detail(detail, *first)
        elif unexpected_skips:
            detail += f"; unexpected skip: {unexpected_skips[0]}"
    return Check(selector, status, clip_diagnostic(detail, _DETAIL_LIMIT))


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
    nodeids: Sequence[str],
    *,
    env: Mapping[str, str],
    expected_skips: Mapping[str, str],
    timeout: float = 900.0,
    pytest_args: Sequence[str] = (),
) -> tuple[Check, ...]:
    """Aggregate pytest cases under each file, directory, class, or test selector.
    Failures win; missing cases, errors, and unexpected skips are errors; all pass is pass.
    All skip is skip only when the selector is expected; exit 77 is never green.
    ``root`` is a disposable directory (a code copy), and pytest leaves no bytecode or cache in it.
    """
    if not nodeids:
        return ()
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
            *nodeids,
        ]
        try:
            run = run_group(argv, cwd=root, env=_environment(root, env), timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            detail = clip_diagnostic(str(exc), _DETAIL_LIMIT, tail=True)
            return tuple(Check(nodeid, "error", detail) for nodeid in nodeids)
        if run.returncode not in (0, 1) or not xml.exists():
            detail = clip_diagnostic(run.stdout + "\n" + run.stderr, _DETAIL_LIMIT - 20, tail=True)
            return tuple(
                Check(nodeid, "error", f"pytest exit {run.returncode}: {detail}")
                for nodeid in nodeids
            )
        try:
            cases = {_nodeid(case): case for case in ET.parse(xml).iter("testcase")}
        except ET.ParseError as exc:
            return tuple(Check(nodeid, "error", f"invalid junit XML: {exc}") for nodeid in nodeids)
        return tuple(_check_selector(selector, cases, expected_skips) for selector in nodeids)
