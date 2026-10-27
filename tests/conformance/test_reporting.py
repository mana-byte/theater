"""A failing live scenario reports the logs its stack captured, whichever phase failed."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.harness_compat.contract import (
    SUITE_ENV_BINARY,
    SUITE_ENV_EVIDENCE_DIR,
    SUITE_ENV_HARNESS,
    SUITE_ENV_SANDBOX_ROOT,
    SUITE_ENV_VERSION,
    SuiteDocument,
)
from scripts.harness_compat.process import SANDBOX_ENV
from tests.support.paths import REPO_ROOT

pytest_plugins = ["pytester"]

LIVE_CONFTEST = REPO_ROOT / "tests" / "conformance" / "live" / "conftest.py"

#: Stand-ins for the rig: each writes the log its capture would have copied, then fails its way.
SCENARIOS = """
import pytest

from tests.support.live_rig import LiveSession, evidence_dir_for


def _capture(request, line):
    log = evidence_dir_for(LiveSession.from_environment(), request.node.name) / "daemon.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"2026-10-27 10:15:00,123 WARNING theater.test {line}\\n")


@pytest.fixture
def live(request):
    yield
    _capture(request, "CAPTURED_AT_TEARDOWN")


@pytest.fixture
def unbootable(request):
    _capture(request, "CAPTURED_AT_BOOT")
    pytest.skip("inconclusive: the stack did not boot")


@pytest.fixture
def messy(request):
    yield
    _capture(request, "CAPTURED_BY_MESSY")
    raise RuntimeError("TEARDOWN_FAILED")


@pytest.mark.family("launch")
def test_call_fails_then_teardown_passes(live, obligation):
    assert False, "CALL_FAILED"


@pytest.mark.family("turn")
def test_boot_is_skipped(unbootable, obligation):
    pass


@pytest.mark.family("interrupt")
def test_call_and_teardown_both_fail(messy, obligation):
    assert False, "CALL_FAILED_TOO"
"""


def _session(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    sandbox = pytester.path / "s"
    (sandbox / "e").mkdir(parents=True)
    (sandbox / "th").mkdir()
    binary = pytester.path / "cli"
    binary.write_text("")
    for name, value in {
        SANDBOX_ENV: "token",
        SUITE_ENV_HARNESS: "claude",
        SUITE_ENV_BINARY: str(binary),
        SUITE_ENV_VERSION: "1.0.0",
        SUITE_ENV_SANDBOX_ROOT: str(sandbox),
        SUITE_ENV_EVIDENCE_DIR: str(sandbox / "e"),
        "THEATER_HOME": str(sandbox / "th"),
        "PYTHONPATH": str(REPO_ROOT),
    }.items():
        monkeypatch.setenv(name, value)
    pytester.makeconftest(LIVE_CONFTEST.read_text(encoding="utf-8"))
    return sandbox / "e"


def test_the_captured_cause_reaches_the_inventory_from_every_phase(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = _session(pytester, monkeypatch)
    pytester.makepyfile(test_scenarios=SCENARIOS)

    pytester.runpytest_subprocess("-p", "no:cacheprovider")

    body = json.loads((evidence / "inventory.json").read_text(encoding="utf-8"))
    rows = SuiteDocument.parse(body, inventory=True).rows
    details = {row.family: row.detail for row in rows if row.detail}
    assert details["launch"].startswith("daemon.log: WARNING theater.test CAPTURED_AT_TEARDOWN")
    assert "CALL_FAILED" in details["launch"]
    assert "CAPTURED_AT_BOOT" in details["turn"]  # skipped before the rig ever yielded
    # The teardown error does not replace the failed call, and its own cause is kept.
    assert "CALL_FAILED_TOO" in details["interrupt"]
    assert "teardown:" in details["interrupt"] and "TEARDOWN_FAILED" in details["interrupt"]
    assert all(len(detail) <= 1000 for detail in details.values())


def test_two_scenarios_cannot_share_an_evidence_directory(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(pytester, monkeypatch)
    # The directory name keeps 80 characters, so these two differ only past them.
    pytester.makepyfile(
        test_names=f"""
import pytest

pytestmark = pytest.mark.family("launch")

def test_{"a" * 90}_one(obligation): pass
def test_{"a" * 90}_two(obligation): pass
"""
    )

    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*share the evidence path*"])
