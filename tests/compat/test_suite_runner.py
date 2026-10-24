"""Suite reconciliation: every promised obligation is accounted for or the run is tooling."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.harness_compat.contract import SUITE_DOCUMENT_SCHEMA, Check
from scripts.harness_compat.sandbox import make_sandbox
from scripts.harness_compat.suite_runner import reconcile, suite_environment

SUITE = Check("tests/conformance/live", "pass", "10 passed")


def _entry(id_: str, scenario: str | None = "test_x.py") -> dict[str, Any]:
    return {"id": id_, "family": id_.split(":", maxsplit=1)[0], "scenario": scenario}


def _required(*entries: dict[str, Any], version: str = "1.2.3") -> dict[str, Any]:
    return {
        "schema": SUITE_DOCUMENT_SCHEMA,
        "harness": "acme",
        "version": version,
        "obligations": list(entries),
    }


def _inventory(*rows: tuple[str, str, str | None, str], version: str = "1.2.3") -> dict[str, Any]:
    return {
        "schema": SUITE_DOCUMENT_SCHEMA,
        "harness": "acme",
        "version": version,
        "obligations": [
            {"id": id_, "status": status, "outcome": outcome, "detail": detail}
            for id_, status, outcome, detail in rows
        ],
    }


def _reconcile(required: Any, inventory: Any, *, partial: bool = False):
    return reconcile(SUITE, required, inventory, harness="acme", version="1.2.3", partial=partial)


def test_every_obligation_becomes_one_check_with_its_outcome() -> None:
    report = _reconcile(
        _required(_entry("launch:manual"), _entry("turn:prompt"), _entry("model:model")),
        _inventory(
            ("launch:manual", "executed", "passed", ""),
            ("turn:prompt", "executed", "failed", "AssertionError: nonce missing"),
            ("model:model", "executed", "skipped", "inconclusive: job still running"),
        ),
    )

    assert not report.problems
    assert [(c.name, c.status) for c in report.checks[1:]] == [
        ("conformance:launch:manual", "pass"),
        ("conformance:turn:prompt", "fail"),
        ("conformance:model:model", "error"),
    ]
    assert "nonce missing" in report.checks[2].detail


def test_a_family_without_a_scenario_is_a_declared_gap_not_a_failure() -> None:
    report = _reconcile(
        _required(_entry("steer:steer", scenario=None)),
        _inventory(("steer:steer", "unproven", None, "")),
    )

    assert not report.problems
    assert report.checks[1] == Check("conformance:steer:steer", "skip", "no live scenario yet")


def test_a_scenario_that_never_ran_is_a_tooling_problem_in_a_full_run() -> None:
    required = _required(_entry("mcp:call"))
    inventory = _inventory(("mcp:call", "unproven", None, ""))

    full = _reconcile(required, inventory)
    partial = _reconcile(required, inventory, partial=True)

    assert full.problems == ("obligation mcp:call has a scenario that never ran",)
    assert full.checks[1].status == "error"
    assert not partial.problems
    assert partial.checks[1] == Check(
        "conformance:mcp:call", "skip", "not selected in this partial run"
    )


def test_an_obligation_missing_from_the_inventory_is_a_tooling_problem() -> None:
    report = _reconcile(_required(_entry("launch:yolo")), _inventory())

    assert report.problems == ("obligation launch:yolo is missing from inventory.json",)
    assert report.checks[-1].status == "error"


def test_an_obligation_nobody_promised_is_a_tooling_problem_unless_partial() -> None:
    inventory = _inventory(("extra:x", "executed", "passed", ""))

    assert _reconcile(_required(), inventory).problems
    assert not _reconcile(_required(), inventory, partial=True).problems


def test_a_missing_or_mismatched_document_is_a_tooling_problem() -> None:
    assert len(_reconcile(None, None).problems) == 2
    stale = _required(version="9.9.9")

    report = _reconcile(stale, _inventory())

    assert any("9.9.9" in problem for problem in report.problems)
    assert report.checks[0] is SUITE


def test_the_suite_environment_is_hermetic_and_names_the_sandbox(tmp_path: Path) -> None:
    with make_sandbox("acme", root=tmp_path / "sb") as sb:
        env = suite_environment(sb, harness="acme", binary=Path("/opt/acme"), version="1.2.3")

        assert env["HARNESS_COMPAT_HARNESS"] == "acme"
        assert env["HARNESS_COMPAT_SANDBOX_ROOT"] == str(sb.root)
        assert env["HARNESS_COMPAT_EVIDENCE_DIR"] == str(sb.evidence_dir)
        assert Path(env["THEATER_HOME"]) == sb.root / "theater"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:9"
        assert env["HARNESS_COMPAT_SANDBOX"]
