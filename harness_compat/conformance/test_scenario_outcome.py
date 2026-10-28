"""Failure explanations distinguish assertions, infrastructure and unexpected exceptions."""

import subprocess

import pytest

from harness_compat.support.scenario_outcome import failure_outcome


@pytest.mark.parametrize(
    "error,outcome",
    [
        (AssertionError("contract mismatch"), "failed"),
        (pytest.fail.Exception("explicit contract failure"), "failed"),
        (TimeoutError("infrastructure timeout"), "error"),
        (subprocess.CalledProcessError(2, "daemon"), "error"),
        (RuntimeError("unexpected helper exception"), "tooling"),
        (ValueError("broken test data"), "tooling"),
    ],
)
def test_failure_classification(error: BaseException, outcome: str) -> None:
    assert failure_outcome(error) == outcome
