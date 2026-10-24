"""Scenario generation and reconciliation facts for the live compatibility suite.

Scenarios come from the harness manifest, not from this tree: a family with no declared
obligation produces no test, and ``required.json`` is written before anything runs so the
pipeline can tell a missing scenario from a passing one.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from scripts.harness_compat.process import SANDBOX_ENV
from tests.support.live_rig import LiveRig, LiveSession, assert_isolated
from tests.support.obligations import (
    FAMILIES,
    SCHEMA,
    Obligation,
    document,
    obligations_for,
    required,
)
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
    json.dumps(document(SESSION.harness, SESSION.version, REQUIRED), indent=2, sort_keys=True),
    encoding="utf-8",
)

_BY_NODE: dict[str, Obligation] = {}
_PHASES: dict[str, dict[str, str]] = {}


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
    for item in items:
        params = item.callspec.params if hasattr(item, "callspec") else {}
        obligation = params.get("obligation", ...)
        if obligation is None:
            undeclared.append(item)
            continue
        if isinstance(obligation, Obligation):
            _BY_NODE[item.nodeid] = obligation
        kept.append(item)
    if undeclared:
        config.hook.pytest_deselected(items=undeclared)
        items[:] = kept


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    obligation = _BY_NODE.get(report.nodeid)
    if obligation is not None:
        _PHASES.setdefault(obligation.id, {})[report.when or "call"] = report.outcome


def _outcome(phases: dict[str, str]) -> str | None:
    if not phases:
        return None
    if phases.get("call") == "failed":
        return "failed"
    if "failed" in phases.values():
        return "error"
    if "skipped" in phases.values():
        return "skipped"
    return "passed" if phases.get("call") == "passed" else "error"


def pytest_sessionfinish(session: pytest.Session) -> None:
    scenarios = {family.name: family.scenario for family in FAMILIES}
    entries = []
    for obligation in REQUIRED:
        outcome = _outcome(_PHASES.get(obligation.id, {}))
        entries.append(
            {
                "id": obligation.id,
                "family": obligation.family,
                "scenario": scenarios[obligation.family],
                "status": "executed" if outcome is not None else "unproven",
                "outcome": outcome,
            }
        )
    body = {
        "schema": SCHEMA,
        "harness": SESSION.harness,
        "version": SESSION.version,
        "obligations": entries,
    }
    (SESSION.evidence_dir / "inventory.json").write_text(
        json.dumps(body, indent=2, sort_keys=True), encoding="utf-8"
    )


@pytest.fixture(scope="session")
def harness() -> Harness:
    return HARNESS


@pytest.fixture
async def live(request: pytest.FixtureRequest, harness: Harness) -> AsyncIterator[LiveRig]:
    async with LiveRig.open(SESSION, harness, label=request.node.name) as rig:
        yield rig
