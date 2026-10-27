"""Scenario generation and reconciliation facts for the live compatibility suite.

Scenarios come from the harness manifest, not from this tree: a family with no declared
obligation produces no test, and ``required.json`` is written before anything runs so the
pipeline can tell a missing scenario from a passing one.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import AsyncIterator, Generator
from pathlib import Path

import pytest

from scripts.harness_compat.contract import SuiteDocument, SuiteRow
from scripts.harness_compat.diagnostics import FAILURE_DETAIL_CHARS
from scripts.harness_compat.process import SANDBOX_ENV
from tests.support.live_rig import LiveRig, LiveSession, assert_isolated, evidence_dir_for
from tests.support.live_stack import log_excerpt
from tests.support.obligations import (
    FAMILIES,
    Obligation,
    document,
    obligations_for,
    required,
)
from tests.support.scenario_outcome import failure_outcome
from theater import config as theater_config
from theater import harness as harness_registry
from theater import paths as theater_paths
from theater.harness.contracts.harness import Harness
from theater.harness.registry import install

if not os.environ.get(SANDBOX_ENV):
    raise pytest.UsageError("the live suite runs only inside the harness-compat sandbox")

SESSION = LiveSession.from_environment()
assert_isolated(SESSION.sandbox_root, theater_paths.home(), SESSION.evidence_dir)
SESSION.evidence_dir.mkdir(parents=True, exist_ok=True)
# An empty local plugin directory, so a developer's own plugins can never shadow a shipped one.
install.install(
    theater_config.Config(),
    local_dir=Path(tempfile.mkdtemp(prefix="p", dir=SESSION.sandbox_root)),
)
HARNESS: Harness = harness_registry.HARNESSES[SESSION.harness]
REQUIRED = required(HARNESS)
(SESSION.evidence_dir / "required.json").write_text(
    json.dumps(document(SESSION.harness, SESSION.version, REQUIRED).to_json(), indent=2),
    encoding="utf-8",
)

_BY_NODE: dict[str, Obligation] = {}
_PHASES: dict[str, dict[str, str]] = {}
_DETAILS: dict[str, str] = {}
_EVIDENCE: dict[str, Path] = {}
#: The first failing phase gets the room; a later one (teardown after a failed call) a share.
_FIRST_CHARS, _LATER_CHARS = 400, 200


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "family(name): the obligation family a scenario proves")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "obligation" not in metafunc.fixturenames:
        return
    marker = metafunc.definition.get_closest_marker("family")
    assert marker is not None, f"{metafunc.function.__name__} declares no family marker"
    declared = obligations_for(HARNESS, marker.args[0])
    if declared:
        metafunc.parametrize("obligation", declared, ids=[item.detail for item in declared])
    else:
        metafunc.parametrize("obligation", [None], ids=["undeclared"])


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    kept: list[pytest.Item] = []
    undeclared: list[pytest.Item] = []
    owners: dict[Path, str] = {}
    for item in items:
        params = item.callspec.params if hasattr(item, "callspec") else {}
        obligation = params.get("obligation", ...)
        if obligation is None:
            undeclared.append(item)
            continue
        if isinstance(obligation, Obligation):
            _BY_NODE[item.nodeid] = obligation
            evidence = _EVIDENCE[item.nodeid] = evidence_dir_for(SESSION, item.name)
            # Two scenarios sharing a directory would report each other's logs as their own.
            if (other := owners.setdefault(evidence, item.nodeid)) != item.nodeid:
                raise pytest.UsageError(
                    f"{item.nodeid} and {other} share the evidence path {evidence}"
                )
        kept.append(item)
    if undeclared:
        config.hook.pytest_deselected(items=undeclared)
        items[:] = kept


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[object]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.failed and call.when == "call" and call.excinfo is not None:
        report.user_properties.append(("compat_outcome", failure_outcome(call.excinfo.value)))
    return report


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    obligation = _BY_NODE.get(report.nodeid)
    if obligation is None:
        return
    outcome = report.outcome
    if report.failed and report.when == "call":
        outcome = dict(report.user_properties).get("compat_outcome", "tooling")
    _PHASES.setdefault(obligation.id, {})[report.when or "call"] = outcome
    if report.outcome != "passed":
        earlier = _DETAILS.get(obligation.id)
        reason = _detail(report, _FIRST_CHARS if earlier is None else _LATER_CHARS)
        _DETAILS[obligation.id] = (
            reason if earlier is None else f"{earlier} ; {report.when}: {reason}"
        )
    if report.when == "teardown" and obligation.id in _DETAILS:
        # Teardown reports after the fixture finalizers, so the stack's logs are on disk by now.
        excerpt = log_excerpt(_EVIDENCE[report.nodeid])
        if excerpt:
            joined = f"{excerpt} || {_DETAILS[obligation.id]}"
            _DETAILS[obligation.id] = joined[:FAILURE_DETAIL_CHARS]


def _detail(report: pytest.TestReport, limit: int) -> str:
    """The first failing line a human needs, from a failure, an error or a skip reason."""
    if report.skipped and isinstance(report.longrepr, tuple):
        return str(report.longrepr[2])[:limit]
    lines = [line.strip() for line in report.longreprtext.splitlines() if line.strip()]
    causes = [line.removeprefix("E").strip() for line in lines if line.startswith("E ")]
    return " ".join(causes or lines[-3:])[:limit]


def _outcome(phases: dict[str, str]) -> str | None:
    if not phases:
        return None
    if phases.get("call") == "failed":
        return "failed"
    if phases.get("call") == "tooling":
        return "tooling"
    if "failed" in phases.values():
        return "error"
    if "skipped" in phases.values():
        return "skipped"
    return "passed" if phases.get("call") == "passed" else "error"


def pytest_sessionfinish(session: pytest.Session) -> None:
    scenarios = {family.name: family.scenario for family in FAMILIES}
    rows = []
    for obligation in REQUIRED:
        outcome = _outcome(_PHASES.get(obligation.id, {}))
        rows.append(
            SuiteRow(
                obligation.id,
                obligation.family,
                scenarios[obligation.family],
                executed=outcome is not None,
                outcome=outcome,
                detail=_DETAILS.get(obligation.id, ""),
            )
        )
    body = SuiteDocument(SESSION.harness, SESSION.version, tuple(rows)).to_json()
    (SESSION.evidence_dir / "inventory.json").write_text(
        json.dumps(body, indent=2), encoding="utf-8"
    )


@pytest.fixture(scope="session")
def harness() -> Harness:
    return HARNESS


@pytest.fixture
async def live(request: pytest.FixtureRequest, harness: Harness) -> AsyncIterator[LiveRig]:
    obligation = getattr(request.node, "callspec", None) and request.node.callspec.params.get(
        "obligation"
    )
    variant = obligation.variant if isinstance(obligation, Obligation) else None
    async with LiveRig.open(SESSION, harness, label=request.node.name, variant=variant) as rig:
        yield rig
