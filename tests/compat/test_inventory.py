"""The obligation baseline: shrinking at the same plugin is red; a gate bump changes nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.harness_compat.contract import Check, Result
from scripts.harness_compat.evidence import prior_inventories, write_evidence
from scripts.harness_compat.inputs import plugin_digest
from scripts.harness_compat.runner import _check_inventory

pytestmark = pytest.mark.usefixtures("isolated_evidence_cache")

PLUGIN = "theater/harness/builtin/plugins/acme"
GATE = f"{PLUGIN}/compatibility.py"


class Adapter:
    name = "acme"
    gate_literals = ((GATE, ("_CEILING",)),)


def _plugin(root: Path, ceiling: str = "(2, 0, 0)", extra: str = "") -> None:
    (root / PLUGIN).mkdir(parents=True, exist_ok=True)
    (root / GATE).write_text(f"_FLOOR = (1, 0, 0)\n_CEILING = {ceiling}\n{extra}", encoding="utf-8")
    (root / PLUGIN / "manifest.py").write_text("MANIFEST = 1\n", encoding="utf-8")


def _green(root: Path, version: str, inventory: tuple[str, ...], digest: str) -> None:
    result = Result(
        "acme",
        version,
        "green",
        (Check("proof", "pass"),),
        adapter_digest="a",
        inventory=inventory,
        plugin_digest=digest,
    )
    write_evidence(root, result, "local")


def test_a_gate_bump_alone_leaves_the_plugin_digest_unchanged(tmp_path: Path) -> None:
    _plugin(tmp_path, "(2, 0, 0)")
    before = plugin_digest(tmp_path, "acme", Adapter())  # type: ignore[arg-type]

    _plugin(tmp_path, "(2, 3, 4)")
    bumped = plugin_digest(tmp_path, "acme", Adapter())  # type: ignore[arg-type]
    _plugin(tmp_path, "(2, 3, 4)", extra="NEW = 1\n")
    edited = plugin_digest(tmp_path, "acme", Adapter())  # type: ignore[arg-type]

    assert bumped == before
    assert edited != before


def test_obligations_that_vanish_at_an_unchanged_plugin_turn_a_green_red(tmp_path: Path) -> None:
    _plugin(tmp_path)
    digest = plugin_digest(tmp_path, "acme", Adapter())  # type: ignore[arg-type]
    _green(tmp_path, "1.1.0", ("launch:yolo", "turn:prompt", "mcp:call"), digest)
    current = Result(
        "acme",
        "1.2.0",
        "green",
        (Check("proof", "pass"),),
        inventory=("launch:yolo", "turn:prompt"),
    )

    checked = _check_inventory(Adapter(), tmp_path, current)  # type: ignore[arg-type]

    assert checked.verdict == "red"
    assert checked.plugin_digest == digest
    assert checked.checks[-1] == Check(
        "inventory", "fail", "obligations dropped at an unchanged plugin: mcp:call"
    )


def test_a_changed_plugin_resets_the_baseline_but_shows_what_went(tmp_path: Path) -> None:
    _plugin(tmp_path)
    _green(tmp_path, "1.1.0", ("launch:yolo", "mcp:call"), "an older plugin")
    current = Result(
        "acme", "1.2.0", "green", (Check("proof", "pass"),), inventory=("launch:yolo",)
    )

    checked = _check_inventory(Adapter(), tmp_path, current)  # type: ignore[arg-type]

    assert checked.verdict == "green"
    assert checked.checks[-1].status == "skip" and "mcp:call" in checked.checks[-1].detail


def test_an_unchanged_or_grown_inventory_passes_untouched(tmp_path: Path) -> None:
    _plugin(tmp_path)
    digest = plugin_digest(tmp_path, "acme", Adapter())  # type: ignore[arg-type]
    _green(tmp_path, "1.1.0", ("launch:yolo",), digest)
    current = Result(
        "acme",
        "1.2.0",
        "green",
        (Check("proof", "pass"),),
        inventory=("launch:yolo", "turn:prompt"),
    )

    checked = _check_inventory(Adapter(), tmp_path, current)  # type: ignore[arg-type]

    assert checked.verdict == "green" and checked.checks == current.checks


def test_only_green_evidence_with_an_inventory_counts_and_the_release_itself_is_excluded(
    tmp_path: Path,
) -> None:
    _green(tmp_path, "1.1.0", ("launch:yolo",), "d")
    _green(tmp_path, "1.2.0", ("launch:yolo", "turn:prompt"), "d")
    plain = Result("acme", "1.3.0", "green", (Check("proof", "pass"),), adapter_digest="a")
    write_evidence(tmp_path, plain, "local")

    found = prior_inventories(tmp_path, "acme", "d", exclude="1.2.0")

    assert found == {"1.1.0": (True, {"launch:yolo"})}
