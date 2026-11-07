"""Run the shipped Claude native plugin's own tests through `claude plugin test`."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support.paths import REPO_ROOT

PLUGIN_DIR = REPO_ROOT / "theater/harness/builtin/plugins/claude/native_plugin"
PLUGIN_TESTS_DIR = REPO_ROOT / "tests/harness/claude/native_plugin"


@pytest.mark.integration
def test_claude_native_plugin_conformance(tmp_path: Path) -> None:
    """Copy the shipped plugin plus its tests to a scratch dir and run them there."""
    if shutil.which("claude") is None:
        pytest.skip("claude is not on PATH")
    plugin = tmp_path / "plugin"
    shutil.copytree(PLUGIN_DIR, plugin)
    shutil.copytree(PLUGIN_TESTS_DIR, plugin / "tests")
    result = subprocess.run(
        ["claude", "plugin", "test", str(plugin)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
