"""The harness-compat contract: gate semantics, verdict rules, evidence discovery."""

from __future__ import annotations

import pytest

from scripts.harness_compat.contract import (
    Check,
    VerifiedSet,
    evidence_file,
    is_stable,
    load_evidenced,
    verdict_from_checks,
)


@pytest.mark.parametrize(
    ("gate", "version", "admitted"),
    [
        (VerifiedSet("exact", versions=frozenset({"0.159.1"})), "0.159.1", True),
        (VerifiedSet("exact", versions=frozenset({"0.159.1"})), "0.160.0", False),
        (VerifiedSet("range", floor="2.24.0", ceiling="2.27.0"), "2.26.9", True),
        (VerifiedSet("range", floor="2.24.0", ceiling="2.27.0"), "2.27.0", False),
        (VerifiedSet("range", floor="2.24.0", ceiling="2.27.0"), "2.23.9", False),
        (VerifiedSet("range", floor="0.80.8", known_bad=frozenset({"0.85.0"})), "0.85.0", False),
        (VerifiedSet("range", floor="2.1.119"), "9.9.9", True),
    ],
)
def test_gate_admits(gate: VerifiedSet, version: str, admitted: bool) -> None:
    assert gate.admits(version) is admitted


@pytest.mark.parametrize(
    ("statuses", "verdict"),
    [
        (("pass", "pass"), "green"),
        (("pass", "skip"), "green"),
        (("pass", "fail"), "red"),
        (("pass", "error"), "inconclusive"),
        (("skip",), "inconclusive"),
        ((), "inconclusive"),
    ],
)
def test_verdict_from_checks(statuses: tuple[str, ...], verdict: str) -> None:
    checks = [Check(f"t{index}", status) for index, status in enumerate(statuses)]  # type: ignore[arg-type]
    assert verdict_from_checks(checks) == verdict


def test_evidence_files_define_evidenced(tmp_path) -> None:
    assert load_evidenced(tmp_path, "vibe") == frozenset()
    path = evidence_file(tmp_path, "vibe", "2.26.1")
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    assert load_evidenced(tmp_path, "vibe") == frozenset({"2.26.1"})
    assert is_stable("2.26.1") and not is_stable("0.163.0-alpha.10")
