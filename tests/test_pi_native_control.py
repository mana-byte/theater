"""Runs the Pi native-control correlation proof against the installed stock Pi.

Exit 77 means the installed release is outside the qualified range, so the
proof skips rather than fails; any other non-zero exit is a regression of a
proven correlation property that gates native send and queue delivery.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.pi.runtime_constants import PI_FRONTEND_QUALIFIED_RANGE
from theater.harness.builtin.plugins.pi.runtime_probe import probe_pi_frontend_compatibility
from theater.harness.contracts.runtime import RuntimeProbeContext

pytestmark = pytest.mark.integration


def _require_qualified_pi() -> None:
    compatibility = probe_pi_frontend_compatibility(RuntimeProbeContext(binary="pi"))
    if not compatibility.supported:
        pytest.skip(f"installed Pi is outside the qualified range {PI_FRONTEND_QUALIFIED_RANGE}")


def test_pi_core_correlation_proof_holds_on_stock_pi() -> None:
    # pytest.skip is NoReturn-typed, so the ``or`` keeps the command list str-only.
    node = shutil.which("node") or pytest.skip("node is unavailable")
    _require_qualified_pi()
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "tests/fixtures/pi_native_control/pi_core_correlation_proof.mts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode == 77:
        pytest.skip("stock Pi is unavailable")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi core correlation proof: ok" in result.stdout


def test_pi_active_run_interrupt_proof_holds_on_stock_pi() -> None:
    node = shutil.which("node") or pytest.skip("node is unavailable")
    _require_qualified_pi()
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "tests/fixtures/pi_native_control/pi_active_run_interrupt_proof.mts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode == 77:
        pytest.skip("stock Pi is unavailable")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi active-run interrupt proof: ok" in result.stdout


def test_pi_steer_admission_proof_holds_on_stock_pi() -> None:
    node = shutil.which("node") or pytest.skip("node is unavailable")
    _require_qualified_pi()
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            node,
            "--experimental-strip-types",
            "tests/fixtures/pi_native_control/pi_steer_admission_proof.mts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode == 77:
        pytest.skip("stock Pi is unavailable")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi steer admission proof: ok" in result.stdout
