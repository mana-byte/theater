"""Suite reconciliation: every promised obligation is accounted for or the run is tooling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.harness_compat.contract import (
    SUITE_DOCUMENT_SCHEMA,
    Check,
    SuiteDocument,
    SuiteDocumentError,
    SuiteRow,
)
from scripts.harness_compat.sandbox import make_sandbox
from scripts.harness_compat.suite_runner import _load, reconcile, suite_environment

SUITE = Check("tests/conformance/live", "pass", "10 passed")


def _entry(id_: str, scenario: str = "test_x.py") -> SuiteRow:
    return SuiteRow(id_, id_.split(":", maxsplit=1)[0], scenario)


def _required(*rows: SuiteRow, version: str = "1.2.3") -> SuiteDocument:
    return SuiteDocument("acme", version, rows)


def _inventory(*rows: tuple[str, str, str | None, str], version: str = "1.2.3") -> SuiteDocument:
    return SuiteDocument(
        "acme",
        version,
        tuple(
            SuiteRow(
                id_,
                id_.split(":", maxsplit=1)[0],
                "test_x.py",
                status == "executed",
                outcome,
                detail,
            )
            for id_, status, outcome, detail in rows
        ),
    )


def _reconcile(
    required: SuiteDocument | str, inventory: SuiteDocument | str, *, partial: bool = False
):
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
    gone = "required.json is missing or unreadable: the suite did not run to the end"
    assert _reconcile(gone, gone).problems == (gone, gone)
    stale = _required(version="9.9.9")

    report = _reconcile(stale, _inventory())

    assert any("9.9.9" in problem for problem in report.problems)
    assert report.checks[0] is SUITE


def _written(tmp_path: Path, body: object, *, name: str = "inventory.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _row(id_: str, **fields: object) -> dict[str, object]:
    return {"id": id_, "family": id_.split(":", maxsplit=1)[0], "scenario": "test_x.py", **fields}


def _body(*rows: dict[str, object], schema: int = SUITE_DOCUMENT_SCHEMA) -> dict[str, object]:
    return {"schema": schema, "harness": "acme", "version": "1.2.3", "obligations": list(rows)}


def test_a_document_round_trips_through_its_one_contract() -> None:
    document = _inventory(
        ("mcp:call", "executed", "failed", "AssertionError"), ("steer:steer", "unproven", None, "")
    )

    assert SuiteDocument.parse(document.to_json(), inventory=True) == document
    required = _required(_entry("mcp:call"))
    assert SuiteDocument.parse(required.to_json(), inventory=False) == required


@pytest.mark.parametrize(
    ("body", "inventory", "reason"),
    [
        pytest.param(_body(schema=2), False, "has schema 2", id="schema"),
        pytest.param(_body(_row("a:b"), _row("a:b")), False, "a:b more than once", id="duplicate"),
        pytest.param(
            _body(_row("a:b", status="executed", outcome="failed", detail="")),
            False,
            None,
            id="a-promise-may-carry-an-account",
        ),
        pytest.param(_body(_row("a:b")), True, "the status None", id="inventory-without-status"),
        pytest.param(
            _body(_row("a:b", status="executed", outcome=None, detail="")),
            True,
            "executed a:b the outcome None",
            id="executed-without-outcome",
        ),
        pytest.param(
            _body(_row("a:b", status="unproven", outcome="passed", detail="")),
            True,
            "unproven a:b the outcome 'passed'",
            id="unproven-with-outcome",
        ),
        *(
            pytest.param(
                _body(_row("a:b", status="unproven", outcome=garbage, detail="")),
                True,
                "unproven a:b the outcome",
                id=f"unproven-with-{garbage!r}",
            )
            for garbage in ("garbage", [], 42)
        ),
        pytest.param(
            _body({"id": "a:b", "family": "a"}), False, "no scenario module name", id="no-scenario"
        ),
        pytest.param(
            _body(_row("a:b", scenario=7)), False, "no scenario module name", id="odd-scenario"
        ),
        pytest.param(
            _body(_row("a:b", scenario=None)), False, "no scenario module name", id="null-scenario"
        ),
    ],
)
def test_a_document_that_contradicts_itself_cannot_be_trusted(
    body: dict[str, object], inventory: bool, reason: str | None
) -> None:
    if reason is None:
        SuiteDocument.parse(body, inventory=inventory)
        return
    with pytest.raises(SuiteDocumentError, match=reason):
        SuiteDocument.parse(body, inventory=inventory)


def test_a_failed_then_passed_duplicate_is_a_reconciliation_problem_not_a_green(
    tmp_path: Path,
) -> None:
    rows = (
        _row("mcp:call", status="executed", outcome="failed", detail="boom"),
        _row("mcp:call", status="executed", outcome="passed", detail=""),
    )

    loaded = _load(_written(tmp_path, _body(*rows)), inventory=True)

    assert isinstance(loaded, str) and "inventory.json lists mcp:call more than once" in loaded


def test_an_unreadable_file_is_the_suite_not_running_to_the_end(tmp_path: Path) -> None:
    (tmp_path / "inventory.json").write_text("{not json", encoding="utf-8")

    assert "did not run to the end" in str(_load(tmp_path / "inventory.json", inventory=True))
    assert "did not run to the end" in str(_load(tmp_path / "absent.json", inventory=False))


def test_the_suite_environment_is_hermetic_and_names_the_sandbox(tmp_path: Path) -> None:
    with make_sandbox("acme", root=tmp_path / "sb") as sb:
        env = suite_environment(sb, harness="acme", binary=Path("/opt/acme"), version="1.2.3")

        assert env["HARNESS_COMPAT_HARNESS"] == "acme"
        assert env["HARNESS_COMPAT_SANDBOX_ROOT"] == str(sb.root)
        assert env["HARNESS_COMPAT_EVIDENCE_DIR"] == str(sb.evidence_dir)
        assert Path(env["THEATER_HOME"]) == sb.root / "theater"
        assert env["HTTPS_PROXY"] == "http://127.0.0.1:9"
        assert env["HARNESS_COMPAT_SANDBOX"]


def test_an_inventory_row_that_dropped_its_scenario_cannot_turn_an_unrun_obligation_green(
    tmp_path: Path,
) -> None:
    unproven = _row("mcp:call", status="unproven", outcome=None, detail="")
    del unproven["scenario"]

    loaded = _load(_written(tmp_path, _body(unproven)), inventory=True)

    assert isinstance(loaded, str) and "no scenario module name" in loaded
