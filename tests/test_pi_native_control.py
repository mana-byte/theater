"""Runs the Pi native-control correlation proof against the installed stock Pi.

Exit 77 means the installed release is outside the supported range, so the
proof skips rather than fails; any other non-zero exit is a regression of a
proven correlation property that gates native send and queue delivery.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_pi_core_correlation_proof_holds_on_stock_pi() -> None:
    # pytest.skip is NoReturn-typed, so the ``or`` keeps the command list str-only.
    node = shutil.which("node") or pytest.skip("node is unavailable")
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            node,
            "--experimental-transform-types",
            "tests/fixtures/pi_native_control/pi_core_correlation_proof.mts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode == 77:
        pytest.skip("installed Pi is outside the supported 0.84.x range")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi core correlation proof: ok" in result.stdout


def test_pi_active_run_interrupt_proof_holds_on_stock_pi() -> None:
    node = shutil.which("node") or pytest.skip("node is unavailable")
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            node,
            "--experimental-transform-types",
            "tests/fixtures/pi_native_control/pi_active_run_interrupt_proof.mts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode == 77:
        pytest.skip("installed Pi is outside the supported 0.84.x range")
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi active-run interrupt proof: ok" in result.stdout
