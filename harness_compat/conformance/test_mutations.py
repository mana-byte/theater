"""The canary catalogue is well formed, and each mutation breaks only what a harness declares."""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from harness_compat.support.mutations import (
    ANY,
    MUTATIONS,
    applicable,
    install,
    package_source,
    undecided,
)
from harness_compat.support.obligations import Obligation, required
from tests.support.fake_runtime import fake_runtime_manifest
from tests.support.manifest_rig import manifest, plan
from theater.harness import HARNESSES
from theater.harness.contracts.manifest import ControlManifest, InterruptPlan, LaunchManifest


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda item: item.name)
def test_every_mutation_generates_a_package_python_can_compile(mutation, tmp_path: Path) -> None:
    source = package_source("acme", mutation)

    compile(source, f"{mutation.name}.py", "exec")
    install(mutation.name, "acme", tmp_path)

    assert (tmp_path / "acme" / "manifest.py").read_text(encoding="utf-8") == source
    assert f"canary {mutation.name}" in source or "canary %s active" in source


def test_a_mutation_names_the_scenarios_that_can_notice_it() -> None:
    for mutation in MUTATIONS:
        assert mutation.select and mutation.breaks, mutation.name
        assert (ANY in mutation.breaks) or all(":" in name for name in mutation.breaks)


def test_a_mutation_applies_only_to_obligations_the_harness_derives() -> None:
    manual = next(item for item in MUTATIONS if item.name == "manual-as-yolo")
    both = (Obligation("mcp", "gated"), Obligation("edit", "gated"), Obligation("turn", "prompt"))

    assert set(applicable(manual, both)) == {"mcp:gated", "edit:gated"}
    assert set(applicable(manual, both[2:])) == set()


def test_a_blanket_mutation_requires_a_plugin_declaration() -> None:
    blanket = next(item for item in MUTATIONS if ANY in item.breaks)

    assert applicable(blanket, (), (blanket.name,)) == blanket.breaks
    assert not applicable(blanket, ())


@pytest.mark.parametrize(
    ("statuses", "expected", "again"),
    [
        pytest.param({"model:model": "error"}, {"model:model": "fail"}, True, id="no-verdict"),
        pytest.param({"model:model": "pass"}, {"model:model": "fail"}, False, id="a-miss"),
        pytest.param(
            {"resume:resume": "error"}, {"resume:resume": "not-pass"}, False, id="lenient"
        ),
        pytest.param(
            {"edit:gated": "fail", "mcp:call": "error"},
            {"edit:gated": "fail"},
            False,
            id="untouched",
        ),
    ],
)
def test_a_canary_run_is_repeated_only_when_a_needed_obligation_ended_without_a_verdict(
    statuses: dict[str, str], expected: dict[str, str], again: bool
) -> None:
    assert undecided(statuses, expected) is again


def test_a_refused_native_runtime_breaks_the_route_and_what_must_survive_on_it() -> None:
    refused = next(item for item in MUTATIONS if item.name == "native-runtime-refused")

    expected = applicable(refused, required(HARNESSES["codex"]))

    assert dict(expected) == {
        "runtime:native": "fail",
        "runtime:survival": "fail",
        "runtime:queue": "fail",
        "runtime:settings": "fail",
    }


def _mutate(name: str, manifest):
    """Run the named mutation's ``mutate`` on ``manifest``, as the sandbox's local plugin does."""
    mutation = next(item for item in MUTATIONS if item.name == name)
    namespace: dict[str, Any] = {"replace": dataclasses.replace, "logging": logging}
    exec(dedent(mutation.source), namespace)
    return namespace["mutate"](manifest)


def _with_native_runtime():
    """A manifest whose legacy planner and native planner and factory each record what they got."""
    seen: dict[str, Any] = {}
    runtime = fake_runtime_manifest()

    def legacy(context):
        seen["legacy"] = context
        return plan(context)

    def backend(context):
        seen["backend"] = context
        return runtime.plan(context)

    def factory(context):
        seen["factory"] = context
        return runtime.factory(context)

    launch = LaunchManifest(planner=legacy, approvals=("manual", "edits", "yolo"))
    controls = ControlManifest(interrupt=InterruptPlan(keys=("Escape",)))
    base = manifest(launch=launch, controls=controls)
    return dataclasses.replace(
        base, runtime=dataclasses.replace(runtime, plan=backend, factory=factory)
    ), seen


def _everywhere_the_manifest_receives_launch_facts(mutated, *, approval: str, model: str) -> None:
    from tests.support.fake_runtime import fake_runtime_context
    from theater.harness.contracts.callbacks import LaunchContext
    from theater.harness.contracts.runtime import RuntimePlanningContext

    mutated.launch.planner(
        LaunchContext(
            participant_id="p1",
            prompt="",
            config_path=Path("/tmp/c.json"),
            approval=approval,
            model=model,
        )
    )
    mutated.runtime.plan(
        RuntimePlanningContext(
            participant_id="p1",
            cwd="/tmp",
            endpoint="unix:///tmp/s",
            approval=approval,
            model=model,
        )
    )
    mutated.runtime.factory(
        dataclasses.replace(fake_runtime_context(), approval=approval, model=model)
    )


@pytest.mark.parametrize(
    ("name", "asked", "received"),
    [
        pytest.param("manual-as-yolo", "manual", "yolo", id="manual-as-yolo"),
        pytest.param("manual-as-yolo", "edits", "edits", id="other-approvals-untouched"),
        pytest.param("edits-as-manual", "edits", "manual", id="edits-as-manual"),
    ],
)
def test_an_approval_canary_breaks_the_legacy_planner_and_the_native_planner_and_runtime(
    name: str, asked: str, received: str
) -> None:
    mutated, seen = _with_native_runtime()
    mutated = _mutate(name, mutated)

    _everywhere_the_manifest_receives_launch_facts(mutated, approval=asked, model="m")

    assert {key: context.approval for key, context in seen.items()} == {
        "legacy": received,
        "backend": received,
        "factory": received,
    }


def test_the_model_canary_drops_the_model_on_every_path() -> None:
    mutated, seen = _with_native_runtime()
    mutated = _mutate("model-dropped", mutated)

    _everywhere_the_manifest_receives_launch_facts(mutated, approval="yolo", model="mock-a")

    assert {key: context.model for key, context in seen.items()} == {
        "legacy": None,
        "backend": None,
        "factory": None,
    }


async def test_the_interrupt_canary_silences_the_pane_keys_and_the_native_interrupt() -> None:
    from tests.support.fake_runtime import fake_runtime_context
    from theater.harness.contracts.runtime import DeliveryResult

    mutated, _seen = _with_native_runtime()
    mutated = _mutate("interrupt-inert", mutated)

    assert mutated.controls.interrupt.keys == ("F20",)
    runtime = mutated.runtime.factory(fake_runtime_context())
    receipt = await runtime.interrupt(operation_id="op-1")
    assert (receipt.result, receipt.error_code) == (DeliveryResult.REJECTED, "canary")


def test_a_manifest_without_a_native_runtime_is_mutated_through_its_legacy_planner_alone() -> None:
    base = manifest(launch=LaunchManifest(planner=plan, approvals=("manual", "yolo")))

    mutated = _mutate("manual-as-yolo", base)

    assert mutated.runtime is None and mutated.launch.planner is not plan


def test_canary_applicability_is_declared_by_the_plugin() -> None:
    env = next(item for item in MUTATIONS if item.name == "launch-env-dropped")
    files = next(item for item in MUTATIONS if item.name == "launch-files-dropped")
    vibe = HARNESSES["vibe"].compat.canaries
    claude = HARNESSES["claude"].compat.canaries

    assert applicable(env, (), vibe) and not applicable(env, (), claude)
    assert applicable(files, (), claude) and not applicable(files, (), vibe)
