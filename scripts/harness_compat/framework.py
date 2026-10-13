"""Subprocess pytest and bump proofs."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
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


def run_pytest(
    root: Path,
    nodeids: Sequence[str],
    *,
    env: Mapping[str, str],
    expected_skips: Mapping[str, str],
    timeout: float = 900.0,
) -> tuple[Check, ...]:
    """Run ``nodeids`` with pytest inside the tree at ``root``; one Check per node id.

    pass: it passed; fail: it failed; skip: it skipped and ``expected_skips`` names it;
    error: timeout, collection error, or a skip that ``expected_skips`` does not name.
    A skip or exit code 77 is never a pass.
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
        checks = []
        for nodeid in nodeids:
            case = cases.get(nodeid)
            if case is None:
                matches = [value for key, value in cases.items() if key.endswith(nodeid)]
                case = matches[0] if len(matches) == 1 else None
            if case is None:
                checks.append(Check(nodeid, "error", "pytest did not report this node id"))
            elif (failure := case.find("failure")) is not None:
                checks.append(Check(nodeid, "fail", (failure.get("message") or "failed")[:500]))
            elif (error := case.find("error")) is not None:
                checks.append(Check(nodeid, "error", (error.get("message") or "error")[:500]))
            elif (skip := case.find("skipped")) is not None:
                status: CheckStatus = "skip" if nodeid in expected_skips else "error"
                checks.append(Check(nodeid, status, (skip.get("message") or "skipped")[:500]))
            else:
                checks.append(Check(nodeid, "pass"))
        return tuple(checks)


def check_bump(adapter: Adapter, root: Path, bumped: Sequence[str]) -> list[str]:
    """Problems with a bump under ``root``; an empty list means it is coherent.

    Writes a stub binary printing ``adapter.version_line(v)`` (and ``adapter.stub_help``),
    runs the real ``adapter.probe`` against the edited tree in a subprocess, and requires
    support for every version in ``bumped``, none for the next release above, and no leftover
    string of the old limit anywhere in the changed files.
    """
    return check_bump_problems(adapter, root, bumped)
