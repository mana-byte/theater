"""The canary catalogue is well formed, and each mutation breaks only what a harness declares."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.mutations import ANY, MUTATIONS, applicable, install, package_source
from tests.support.obligations import Obligation


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


def test_a_blanket_mutation_applies_wherever_the_suite_runs() -> None:
    blanket = next(item for item in MUTATIONS if ANY in item.breaks)

    assert applicable(blanket, ()) == blanket.breaks
