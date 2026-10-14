"""Subprocess pytest and bump proofs."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from .contract import Adapter, Check, CheckStatus
from .probe import check_bump_problems


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
        if unexpected_skips:
            detail += f"; unexpected skip: {', '.join(unexpected_skips[:3])}"
    elif counts["passed"] == len(selected):
        status = "pass"
    elif counts["skipped"] == len(selected) and selector in expected_skips:
        status = "skip"
    else:
        status = "error"
        detail += "; selector did not fully pass or skip"
    return Check(selector, status, detail[:500])


def run_pytest(
    root: Path,
    nodeids: Sequence[str],
    *,
    env: Mapping[str, str],
    expected_skips: Mapping[str, str],
    timeout: float = 900.0,
) -> tuple[Check, ...]:
    """Aggregate pytest cases under each file, directory, class, or test selector.
    Failures win; missing cases, errors, and unexpected skips are errors; all pass is pass.
    All skip is skip only when the selector is expected; exit 77 is never green.
    """
    if not nodeids:
        return ()
    with tempfile.TemporaryDirectory(prefix="compat-pytest-") as temporary:
        xml = Path(temporary) / "junit.xml"
        argv = [sys.executable, "-m", "pytest", "-q", f"--junitxml={xml}", *nodeids]
        try:
            run = subprocess.run(
                argv,
                cwd=root,
                env={**env, "PYTHONPATH": str(root)},
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return tuple(Check(nodeid, "error", str(exc)) for nodeid in nodeids)
        if run.returncode not in (0, 1) or not xml.exists():
            detail = (run.stdout + "\n" + run.stderr)[-1000:].strip()
            return tuple(
                Check(nodeid, "error", f"pytest exit {run.returncode}: {detail}")
                for nodeid in nodeids
            )
        try:
            cases = {_nodeid(case): case for case in ET.parse(xml).iter("testcase")}
        except ET.ParseError as exc:
            return tuple(Check(nodeid, "error", f"invalid junit XML: {exc}") for nodeid in nodeids)
        return tuple(_check_selector(selector, cases, expected_skips) for selector in nodeids)


def check_bump(adapter: Adapter, root: Path, bumped: Sequence[str]) -> list[str]:
    """Probe edited tree in a subprocess with version_line and stub_help.
    Bumped versions must be supported; the next release must be refused.
    Active old bounds fail; historical references become review warnings.
    """
    return check_bump_problems(adapter, root, bumped)
