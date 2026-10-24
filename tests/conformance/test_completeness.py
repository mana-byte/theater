"""The live suite's obligations follow each shipped manifest, and no family is silently dropped."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.obligations import FAMILIES, document, required
from theater.harness import HARNESSES
from theater.harness.builtin import plugin_dir

LIVE = Path(__file__).parent / "live"
#: Read from disk: the registry is only populated after collection.
SHIPPED = sorted(path.parent.name for path in plugin_dir().glob("*/manifest.py"))


def test_every_family_names_a_scenario_module_that_exists_or_says_why_not() -> None:
    for family in FAMILIES:
        if family.scenario is None:
            assert family.reason, f"family {family.name!r} has neither a scenario nor a reason"
        else:
            assert (LIVE / family.scenario).is_file(), family.scenario


def test_every_scenario_module_belongs_to_one_declared_family() -> None:
    claimed = {family.scenario for family in FAMILIES if family.scenario}
    modules = {path.name for path in LIVE.glob("test_*.py")}

    assert modules == claimed


def test_every_scenario_is_marked_with_its_own_family() -> None:
    for family in FAMILIES:
        if family.scenario is None:
            continue
        source = (LIVE / family.scenario).read_text(encoding="utf-8")
        assert f'pytest.mark.family("{family.name}")' in source, family.scenario


@pytest.mark.parametrize("name", SHIPPED)
def test_a_shipped_harness_derives_obligations_only_from_what_it_declares(name: str) -> None:
    harness = HARNESSES[name]
    derived = {item.id for item in required(harness)}

    support = harness.launch_parameter_support
    assert {f"launch:{approval}" for approval in support.approvals} <= derived
    assert ("model:model" in derived) is support.model
    assert ("resume:resume" in derived) is support.resume
    assert ("effort:effort" in derived) is support.reasoning_effort
    assert ("interrupt:interrupt" in derived) is (
        harness.controls is not None and harness.controls.interrupt is not None
    )
    assert any(item.startswith("mcp:") for item in derived) is harness.supports_mcp_rendering
    assert any(item.startswith("edit:") for item in derived) is (
        harness.compat is not None and harness.compat.native_edit is not None
    )
    assert "turn:prompt" in derived


@pytest.mark.parametrize("name", SHIPPED)
def test_the_required_document_lists_every_obligation_once(name: str) -> None:
    harness = HARNESSES[name]

    body = document(name, "1.2.3", required(harness))

    ids = [entry["id"] for entry in body["obligations"]]
    assert len(ids) == len(set(ids))
    assert ids == [item.id for item in required(harness)]
    assert {entry["family"] for entry in body["obligations"]} <= {f.name for f in FAMILIES}


def test_the_shipped_plugins_are_all_discovered() -> None:
    assert set(SHIPPED) == set(HARNESSES)
