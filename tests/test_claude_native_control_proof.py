"""Claude Code native-control Phase 0 proof: no public surface qualifies, so
the shipped manifest must stay fail-closed and the recorded verdicts honest.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.claude.manifest import MANIFEST, manifest_for_root
from theater.harness.contracts.manifest import InterruptPlan
from theater.harness.contracts.runtime import RuntimeCapability

FIXTURES = Path(__file__).parent / "fixtures" / "claude_native_control"
PROOF_ENV = "THEATER_CLAUDE_NATIVE_PROOF"
PROBE_ENABLED = os.environ.get(PROOF_ENV) == "1" and bool(shutil.which("claude"))
DRIFT_MESSAGE = (
    "the installed Claude Code surface drifted from the Phase 0 proof; re-run "
    "the spike in docs/native-interaction/claude.md before implementing any runtime"
)

_CONTROL_CAPABILITIES = (
    RuntimeCapability.SEND,
    RuntimeCapability.QUEUE_FOLLOWUP,
    RuntimeCapability.STEER,
    RuntimeCapability.INTERRUPT,
    RuntimeCapability.SETTINGS_UPDATE,
)


def test_shipped_manifest_stays_fail_closed():
    assert MANIFEST.runtime is None
    assert MANIFEST.controls.interrupt == InterruptPlan(keys=("Escape",))
    for_root = manifest_for_root(Path("/anywhere"))
    assert for_root.runtime is None
    assert for_root.controls.interrupt == InterruptPlan(keys=("Escape",))


def test_recorded_capability_verdicts_fail_closed():
    facts = json.loads((FIXTURES / "public_surface.json").read_text())
    for name, candidate in facts["candidates"].items():
        assert candidate["verdict"] in ("fail", "not_stock_tui"), name
        assert candidate["gates_failed"], name
    decisions = facts["capability_decisions"]
    for capability in _CONTROL_CAPABILITIES:
        assert decisions[capability.value] == "fail", capability
    assert decisions["observation"] == "retained_hooks_and_transcript"
    assert facts["native_turn_id"].startswith("none:")


def _option_entry(lines: list[str], flag: str) -> str:
    start = next(i for i, line in enumerate(lines) if line.startswith(f"  {flag}"))
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].startswith("  --")),
        len(lines),
    )
    return "\n".join(lines[start:end])


def _commands(lines: list[str]) -> list[str]:
    body = lines[lines.index("Commands:") + 1 :]
    return [
        entry.strip().split()[0]
        for entry in body
        if entry.startswith("  ") and not entry.startswith("   ") and entry.strip()
    ]


@pytest.mark.skipif(not PROBE_ENABLED, reason=f"set {PROOF_ENV}=1 with a stock claude binary")
def test_native_probe_installed_binary_still_fails_the_attachment_gates():
    completed = subprocess.run(
        ["claude", "--help"], capture_output=True, text=True, timeout=15, check=False
    )
    assert completed.returncode == 0, DRIFT_MESSAGE
    lines = completed.stdout.splitlines()
    input_format = _option_entry(lines, "--input-format")
    assert "stream-json" in input_format and "only works with --print" in input_format, (
        DRIFT_MESSAGE
    )
    commands = _commands(lines)
    assert "connect" not in commands and "attach" not in commands, DRIFT_MESSAGE
