"""The live suite as a shadow result: recorded beside a verdict, never part of it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from scripts.harness_compat import runner, suite_runner
from scripts.harness_compat.contract import Check, Result, Sandbox
from scripts.harness_compat.evidence import reusable_evidence, write_evidence
from scripts.harness_compat.summary import render_harness

pytestmark = pytest.mark.usefixtures("isolated_evidence_cache")

SHADOW = (
    Check("tests/conformance/live", "pass", "10 passed"),
    Check("conformance:turn:prompt", "pass", "passed"),
    Check("conformance:resume:resume", "fail", "AssertionError: history lost"),
    Check("conformance:steer:steer", "skip", "no live scenario yet"),
)


class ShadowedAdapter:
    name = "acme"
    shadow_suite = True
    green_tests = ("tests/proof.py::test_real",)
    expected_skips: ClassVar[dict[str, str]] = {}
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()
    verdict = "green"

    def install(self, sb: Sandbox, version: str) -> Path:
        return sb.bin_dir / "acme"

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        checks = (Check(self.green_tests[0], "pass" if self.verdict == "green" else "fail"),)
        return Result(self.name, version, self.verdict, checks)  # type: ignore[arg-type]


@pytest.fixture
def ran(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake(root: Path, sb: Sandbox, **kwargs: Any) -> tuple[Check, ...]:
        calls.append(kwargs["version"])
        return SHADOW

    monkeypatch.setattr(runner, "shadow_checks", fake)
    return calls


@pytest.mark.parametrize("verdict", ["green", "red"])
def test_a_conclusive_verdict_carries_the_shadow_result_without_being_changed_by_it(
    tmp_path: Path, ran: list[str], verdict: str
) -> None:
    adapter = ShadowedAdapter()
    adapter.verdict = verdict

    result, _ = runner._qualify(adapter, tmp_path, "1.2.3", bump=False)  # type: ignore[arg-type]

    assert result.verdict == verdict
    assert result.shadow_checks == SHADOW
    assert ran == ["1.2.3"]


def test_an_inconclusive_run_or_a_non_opted_in_adapter_runs_no_shadow(
    tmp_path: Path, ran: list[str]
) -> None:
    inconclusive = ShadowedAdapter()
    inconclusive.verdict = "inconclusive"
    plain = ShadowedAdapter()
    plain.shadow_suite = False

    first, _ = runner._qualify(inconclusive, tmp_path, "1.2.3", bump=False)  # type: ignore[arg-type]
    second, _ = runner._qualify(plain, tmp_path, "1.2.3", bump=False)  # type: ignore[arg-type]

    assert first.shadow_checks == second.shadow_checks == ()
    assert ran == []


def test_a_suite_that_crashes_leaves_one_error_check_and_no_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the sandbox vanished")

    monkeypatch.setattr(suite_runner, "run_suite", boom)

    (check,) = suite_runner.shadow_checks(
        tmp_path,
        None,
        harness="acme",
        binary=tmp_path / "acme",
        version="1.2.3",  # type: ignore[arg-type]
    )

    assert (check.name, check.status) == ("shadow", "error")
    assert "the sandbox vanished" in check.detail


def test_a_failing_suite_keeps_its_evidence_where_the_workflow_uploads_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = tmp_path / "e"
    (evidence / "scenario").mkdir(parents=True)
    (evidence / "scenario" / "daemon.log").write_text("log\n")
    (evidence / "huge.log").write_bytes(b"x" * 1_500_000)
    kept = tmp_path / "diagnostics"
    monkeypatch.setenv(suite_runner.DIAGNOSTICS_ENV, str(kept))
    sandbox = type("Sb", (), {"evidence_dir": evidence})()
    monkeypatch.setattr(
        suite_runner,
        "run_suite",
        lambda *args, **kwargs: suite_runner.SuiteReport((Check("conformance:x", "fail", "no"),)),
    )

    suite_runner.shadow_checks(tmp_path, sandbox, harness="acme", binary=tmp_path, version="1.2.3")  # type: ignore[arg-type]

    assert (kept / "acme-1.2.3-suite" / "scenario" / "daemon.log").read_text() == "log\n"
    assert not (kept / "acme-1.2.3-suite" / "huge.log").exists()


def test_a_passing_suite_keeps_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    kept = tmp_path / "diagnostics"
    monkeypatch.setenv(suite_runner.DIAGNOSTICS_ENV, str(kept))
    sandbox = type("Sb", (), {"evidence_dir": tmp_path})()
    monkeypatch.setattr(
        suite_runner,
        "run_suite",
        lambda *args, **kwargs: suite_runner.SuiteReport((Check("conformance:x", "pass"),)),
    )

    suite_runner.shadow_checks(tmp_path, sandbox, harness="acme", binary=tmp_path, version="1.2.3")  # type: ignore[arg-type]

    assert not kept.exists()


def test_evidence_records_the_shadow_only_when_there_is_one_and_reuse_ignores_it(
    tmp_path: Path,
) -> None:
    plain = Result("acme", "1.2.3", "green", (Check("proof", "pass"),), adapter_digest="d")
    shadowed = Result(
        "acme",
        "1.2.4",
        "green",
        (Check("proof", "pass"),),
        adapter_digest="d",
        shadow_checks=SHADOW,
    )

    assert "shadow" not in plain.to_evidence()
    assert [item["name"] for item in shadowed.to_evidence()["shadow"]][:2] == [
        "tests/conformance/live",
        "conformance:turn:prompt",
    ]
    path = write_evidence(tmp_path, shadowed, "local")
    assert json.loads(path.read_text())["shadow"][2]["status"] == "fail"
    assert set(reusable_evidence(tmp_path, "acme", "d", required=("proof",))) == {"1.2.4"}


def test_the_job_summary_lists_the_shadow_beside_the_verdict() -> None:
    row = {
        "version": "1.2.3",
        "verdict": "green",
        "checks": [],
        "shadow": [_as_dict(check) for check in SHADOW],
    }
    summary = {
        "harness": "acme",
        "gate_kind": "range",
        "gate": {"floor": "1.0.0", "ceiling": "2.0.0"},
        "candidates": ["1.2.3"],
        "deferred": [],
        "greens": ["1.2.3"],
        "results": [row],
        "watch": None,
        "watch_reused": False,
        "composition_attempts": [],
        "stale_warnings": [],
    }

    text = render_harness(summary)

    assert "Shadow: live conformance suite (not part of the verdict)" in text
    assert "| 1.2.3 | 2 | 1 | 0 | 1 |" in text
    assert "conformance:resume:resume: AssertionError: history lost" in text


def _as_dict(check: Check) -> dict[str, str]:
    return {"name": check.name, "status": check.status, "detail": check.detail}
