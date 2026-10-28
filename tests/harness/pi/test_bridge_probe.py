"""Pi frontend bridge version probe and the packaged extension."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

import pytest

from tests.support.paths import REPO_ROOT
from theater.harness.builtin.plugins.pi import runtime as pi_runtime_module
from theater.harness.builtin.plugins.pi.runtime_constants import (
    VERSION_POLICY,
)
from theater.harness.contracts.runtime import (
    RuntimeProbeContext,
)


@dataclass
class _Completed:
    stdout: str
    stderr: str = ""
    returncode: int = 0


@pytest.mark.parametrize("suffix", ["+build", "-beta", ".1"])
def test_pi_probe_never_admits_a_stable_prefix_of_an_unstable_release(monkeypatch, suffix):
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: _Completed(f"{VERSION_POLICY.minimum}{suffix}\n")
    )
    refused = pi_runtime_module.probe_pi_frontend_compatibility(RuntimeProbeContext(binary="pi"))
    assert not refused.supported and refused.native_version is None


def test_pi_extension_never_touches_private_pi_surfaces() -> None:
    bridge = (REPO_ROOT / "theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts").read_text(
        encoding="utf-8"
    )

    assert "AgentSession" not in bridge
    assert "setStatus" not in bridge
    assert "await this.pi.setModel(" not in bridge


@pytest.mark.requires_node
def test_pi_extension_frontend_bridge_executable_conformance() -> None:
    """Drive the actual extension registration through fresh public contexts."""
    root = REPO_ROOT
    result = subprocess.run(
        [
            "node",
            "--experimental-strip-types",
            "tests/fixtures/pi_frontend_bridge_conformance.mts",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "pi frontend bridge executable conformance: ok" in result.stdout
