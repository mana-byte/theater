"""The live suite accounts for every declaration a manifest can make, and follows each manifest."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.coverage import COVERAGE, NoLive
from tests.support.declarations import declarations
from tests.support.manifest_rig import manifest, plan
from tests.support.mutations import MUTATIONS
from tests.support.obligations import FAMILIES, document, required
from theater.harness import HARNESSES
from theater.harness.builtin import plugin_dir
from theater.harness.contracts.compat import COMPAT_PROTOCOLS, ProvisionContext
from theater.harness.contracts.manifest import (
    ControlManifest,
    InterruptPlan,
    LaunchManifest,
    McpRenderingManifest,
    SteerPlan,
)
from theater.harness.contracts.runtime import RuntimeHost
from theater.harness.manifests.compiler import compile_manifest

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
    assert ("runtime:survival" in derived) is (harness.runtime is not None)


@pytest.mark.parametrize("name", SHIPPED)
def test_the_required_document_lists_every_obligation_once(name: str) -> None:
    harness = HARNESSES[name]

    body = document(name, "1.2.3", required(harness))

    ids = [row.id for row in body.rows]
    assert len(ids) == len(set(ids))
    assert ids == [item.id for item in required(harness)]
    assert {row.family for row in body.rows} <= {f.name for f in FAMILIES}


def test_the_shipped_plugins_are_all_discovered() -> None:
    assert set(SHIPPED) == set(HARNESSES)


def test_every_declaration_a_manifest_can_make_is_mapped_or_exempted() -> None:
    found = declarations()

    assert not found - set(COVERAGE), (
        "map each of these to a family or a reasoned NoLive in tests/support/coverage.py: "
        f"{sorted(found - set(COVERAGE))}"
    )
    assert not set(COVERAGE) - found, (
        f"the coverage registry names declarations that no longer exist: "
        f"{sorted(set(COVERAGE) - found)}"
    )


def test_every_mapped_family_exists_and_every_family_proves_something() -> None:
    families = {family.name for family in FAMILIES}
    mapped = {covered for covered in COVERAGE.values() if isinstance(covered, str)}

    assert mapped <= families, mapped - families
    assert families <= mapped, f"families that prove no declaration: {families - mapped}"


def test_every_exemption_states_a_reason_that_is_not_a_placeholder() -> None:
    reasons = [covered.reason for covered in COVERAGE.values() if isinstance(covered, NoLive)]

    assert all(len(reason) >= 40 for reason in reasons), reasons


_STEER = SteerPlan()
_INTERRUPT = ControlManifest(interrupt=InterruptPlan(keys=("Escape",)))


def _launch(**support: bool) -> LaunchManifest:
    """The baseline launch declaration (same approvals) with one capability switched on."""
    return LaunchManifest(planner=plan, approvals=("manual", "edits", "yolo"), **support)


@pytest.mark.parametrize(
    ("declared", "adds"),
    [
        pytest.param(
            {"launch": _launch(supports_model=True)},
            {"model:model"},
            id="model",
        ),
        pytest.param(
            {"launch": _launch(supports_resume=True)},
            {"resume:resume"},
            id="resume",
        ),
        pytest.param({"controls": _INTERRUPT}, {"interrupt:interrupt"}, id="interrupt"),
        pytest.param({"controls": ControlManifest(steer=_STEER)}, {"steer:steer"}, id="steer"),
    ],
)
def test_enabling_a_capability_adds_exactly_its_obligations(
    declared: dict[str, object], adds: set[str]
) -> None:
    base = {item.id for item in required(compile_manifest("acme", manifest()))}

    enabled = {item.id for item in required(compile_manifest("acme", manifest(**declared)))}

    assert enabled - base == adds
    assert base <= enabled


def test_enabling_mcp_rendering_adds_the_mcp_obligations() -> None:
    base = compile_manifest("acme", manifest())
    enabled = compile_manifest(
        "acme", replace(manifest(), mcp=McpRenderingManifest(renderer=lambda context: None))
    )

    added = {item.id for item in required(enabled)} - {item.id for item in required(base)}

    assert added == {"mcp:gated", "mcp:call", "mcp:refusal"}


@pytest.mark.parametrize("name", SHIPPED)
def test_a_provisioning_plugin_offers_what_its_declared_capabilities_need(
    name: str, tmp_path: Path
) -> None:
    harness = HARNESSES[name]
    if harness.compat is None:
        pytest.skip(f"{name} declares no compat manifest yet")
    context = ProvisionContext(
        sandbox_root=tmp_path,
        home=tmp_path / "home",
        config_dir=tmp_path / "home" / ".config",
        workdir=tmp_path / "work",
        mock_urls={protocol: f"http://127.0.0.1:1/{protocol}" for protocol in COMPAT_PROTOCOLS},
        binary=tmp_path / name,
        version="1.2.3",
        models=("mock-a", "mock-b"),
    )

    provisioned = harness.compat.provision(context)

    support = harness.launch_parameter_support
    assert provisioned.protocol in COMPAT_PROTOCOLS
    assert not support.model or len(provisioned.models) >= 2, "model selection needs two models"
    assert not support.reasoning_effort or len(provisioned.efforts) >= 2, (
        "effort selection needs two efforts"
    )
    assert all(path.is_relative_to(tmp_path) for path in provisioned.files), (
        "provisioning writes only inside the sandbox"
    )


def test_a_declared_unavailable_capability_becomes_one_refusal_obligation() -> None:
    opencode = {item.id for item in required(HARNESSES["opencode"])}
    pi = {item.id for item in required(HARNESSES["pi"])}

    assert {
        "runtime:native",
        "runtime:survival",
        "runtime:legacy",
        "runtime:unavailable-settings_update",
    } <= opencode
    assert "steer:refused" in opencode
    assert "runtime:unavailable-steer" not in opencode | pi, "steer is proven by its own family"
    assert {"runtime:native", "runtime:survival", "runtime:legacy", "steer:refused"} <= pi
    assert not any(
        item.startswith("runtime:") for item in {i.id for i in required(HARNESSES["vibe"])}
    )


def test_both_runtime_hosts_must_survive_a_daemon_restart() -> None:
    hosts = {
        harness.runtime.host
        for name in SHIPPED
        if (harness := HARNESSES[name]).runtime is not None
        and "runtime:survival" in {item.id for item in required(harness)}
    }

    assert hosts == set(RuntimeHost)


def test_the_legacy_obligation_runs_under_the_variant_that_refuses_the_native_runtime() -> None:
    legacy = next(item for item in required(HARNESSES["codex"]) if item.id == "runtime:legacy")
    native = next(item for item in required(HARNESSES["codex"]) if item.id == "runtime:native")

    assert legacy.variant == "native-runtime-refused"
    assert native.variant is None
    assert legacy.variant in {mutation.name for mutation in MUTATIONS}
