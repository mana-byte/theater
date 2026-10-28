"""A failing live scenario reports the logs its stack captured, even when its teardown passes."""

from __future__ import annotations

import json

import pytest

from harness_compat.contract import (
    SUITE_ENV_BINARY,
    SUITE_ENV_EVIDENCE_DIR,
    SUITE_ENV_HARNESS,
    SUITE_ENV_SANDBOX_ROOT,
    SUITE_ENV_VERSION,
    SuiteDocument,
)
from harness_compat.process import SANDBOX_ENV
from tests.support.paths import REPO_ROOT

pytest_plugins = ["pytester"]

#: Stand-ins for the rig: each writes the log its capture would have copied, then fails its way.
SCENARIOS = """
import pytest

from harness_compat.support.live_rig import LiveSession, evidence_dir_for


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


@pytest.mark.family("launch")
def test_call_fails_then_teardown_passes(live, obligation):
    assert False, "CALL_FAILED"


@pytest.mark.family("turn")
def test_boot_is_skipped(unbootable, obligation):
    pass
"""


def test_the_captured_cause_reaches_the_inventory_whichever_phase_failed(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = pytester.path / "s"
    evidence = sandbox / "e"
    evidence.mkdir(parents=True)
    (sandbox / "th").mkdir()
    binary = pytester.path / "cli"
    binary.write_text("")
    for name, value in {
        SANDBOX_ENV: "token",
        SUITE_ENV_HARNESS: "claude",
        SUITE_ENV_BINARY: str(binary),
        SUITE_ENV_VERSION: "1.0.0",
        SUITE_ENV_SANDBOX_ROOT: str(sandbox),
        SUITE_ENV_EVIDENCE_DIR: str(evidence),
        "THEATER_HOME": str(sandbox / "th"),
        "PYTHONPATH": str(REPO_ROOT),
    }.items():
        monkeypatch.setenv(name, value)
    live_conftest = REPO_ROOT / "harness_compat" / "conformance" / "live" / "conftest.py"
    pytester.makeconftest(live_conftest.read_text(encoding="utf-8"))
    pytester.makepyfile(test_scenarios=SCENARIOS)

    pytester.runpytest_subprocess("-p", "no:cacheprovider")

    body = json.loads((evidence / "inventory.json").read_text(encoding="utf-8"))
    details = {row.family: row.detail for row in SuiteDocument.parse(body, inventory=True).rows}
    assert details["launch"].startswith("daemon.log: WARNING theater.test CAPTURED_AT_TEARDOWN")
    assert "CALL_FAILED" in details["launch"]
    assert "CAPTURED_AT_BOOT" in details["turn"]  # skipped before the rig ever yielded
