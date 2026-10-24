"""Distinct interrupt races through the installed Pi SDK and shipped bridge."""

from __future__ import annotations

import shutil

import pytest

from scripts.harness_compat.process import run_group
from scripts.harness_compat.sandbox import make_sandbox
from tests.support.paths import TESTS_DIR

pytestmark = [pytest.mark.integration, pytest.mark.requires_node]


def test_exact_run_interrupt_retry_backoff_and_session_replacement() -> None:
    node, pi = shutil.which("node"), shutil.which("pi")
    if node is None or pi is None:
        pytest.skip("requires the installed Pi SDK and node")
    with make_sandbox("pi") as sandbox:
        result = run_group(
            [node, "--experimental-strip-types", str(TESTS_DIR / "support" / "pi_interrupt.mts")],
            cwd=sandbox.root,
            env={**sandbox.offline_env, "THEATER_TEST_PI_BINARY": pi},
            timeout=120,
        )
    if result.returncode == 77:
        pytest.skip("the installed Pi package exposes no SDK")
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    assert "pi interrupt regressions: ok" in result.stdout
