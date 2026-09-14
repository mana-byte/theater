"""Claude Code native-control Phase 0 proof (docs/native-interaction/claude.md).

No public surface qualifies, so these tests pin the recorded verdicts, keep the
shipped manifest fail-closed, and (opt-in) re-check the installed stock binary
for drift that would re-open the attachment spike.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from theater.daemon.controls.routing import ControlRouteResolver
from theater.harness.builtin.plugins.claude.manifest import MANIFEST, manifest_for_root
from theater.harness.contracts.channels import ChannelKind
from theater.harness.contracts.manifest import InterruptPlan
from theater.harness.contracts.runtime import ControlTransport, RuntimeCapability

FIXTURES = Path(__file__).parent / "fixtures" / "claude_native_control"
PROOF_ENV = "THEATER_CLAUDE_NATIVE_PROOF"
PROBE_ENABLED = os.environ.get(PROOF_ENV) == "1" and bool(shutil.which("claude"))
DRIFT_MESSAGE = (
    "the installed Claude Code surface drifted from the Phase 0 proof; re-run the "
    "spike in docs/native-interaction/claude.md before implementing any runtime"
)

_CONTROL_CAPABILITIES = (
    RuntimeCapability.SEND,
    RuntimeCapability.QUEUE_FOLLOWUP,
    RuntimeCapability.STEER,
    RuntimeCapability.INTERRUPT,
    RuntimeCapability.SETTINGS_UPDATE,
)


def _facts() -> dict:
    return json.loads((FIXTURES / "public_surface.json").read_text())


def _help_lines() -> list[str]:
    return (FIXTURES / "help_surface.txt").read_text().splitlines()


def _commands(lines: list[str]) -> list[str]:
    body = lines[lines.index("Commands:") + 1 :]
    return [
        entry.strip().split()[0]
        for entry in body
        if entry.startswith("  ") and not entry.startswith("   ") and entry.strip()
    ]


def _option_entry(lines: list[str], flag: str) -> str:
    start = next(i for i, line in enumerate(lines) if line.startswith(f"  {flag}"))
    end = next(
        (j for j in range(start + 1, len(lines)) if lines[j].startswith("  --")),
        len(lines),
    )
    return "\n".join(lines[start:end])


# ---- shipped routing stays fail-closed -------------------------------------


def test_shipped_manifest_declares_no_native_runtime():
    assert MANIFEST.runtime is None
    assert MANIFEST.controls.interrupt == InterruptPlan(keys=("Escape",))
    for_root = manifest_for_root(Path("/anywhere"))
    assert for_root.runtime is None
    assert for_root.controls.interrupt == InterruptPlan(keys=("Escape",))


def test_hooks_are_declared_only_as_observation_enrichments():
    hooks = MANIFEST.observation.hook_channels
    assert [channel.declaration.id for channel in hooks] == ["native-hooks"]
    assert all(channel.declaration.kind is ChannelKind.HOOK for channel in hooks)
    primary = MANIFEST.observation.primary
    assert primary is not None and primary.channel.kind is ChannelKind.TRANSCRIPT


def test_claude_routes_resolve_legacy_or_unavailable_never_native():
    store = SimpleNamespace(
        get_runtime_binding=lambda _pid: None,
        get_participant=lambda _pid: SimpleNamespace(harness="claude"),
    )
    resolver = ControlRouteResolver(store=store, runtime_for=lambda _pid: None)
    for capability in _CONTROL_CAPABILITIES:
        route = resolver.resolve("claude-1", capability)
        assert not route.is_native, capability
        expected = (
            ControlTransport.LEGACY_TMUX
            if capability
            in (
                RuntimeCapability.SEND,
                RuntimeCapability.QUEUE_FOLLOWUP,
                RuntimeCapability.INTERRUPT,
            )
            else None
        )
        assert route.transport is expected, capability
        assert route.native_wiring is False


# ---- recorded proof facts ----------------------------------------------------


def test_proof_facts_fail_every_candidate_with_documented_gates():
    facts = _facts()
    candidates = facts["candidates"]
    assert set(candidates) == {"channels", "remote_control", "sdk_stream_json"}
    for name, candidate in candidates.items():
        assert candidate["verdict"] in ("fail", "not_stock_tui"), name
        assert candidate["gates_failed"], name
        assert all(isinstance(gate, str) and gate for gate in candidate["gates_failed"]), name
    # the acknowledgement gap is the decisive channel gate
    assert "doesn't acknowledge notifications" in " ".join(candidates["channels"]["gates_failed"])


def test_proof_facts_fail_every_control_capability_and_disown_any_turn_id():
    facts = _facts()
    decisions = facts["capability_decisions"]
    for capability in _CONTROL_CAPABILITIES:
        assert decisions[capability.value] == "fail", capability
    assert decisions["observation"] == "retained_hooks_and_transcript"
    assert facts["native_turn_id"].startswith("none:")
    for url in facts["inspected"]["documentation"].values():
        assert url.startswith("https://code.claude.com/docs/")


# ---- captured stock-binary surface -------------------------------------------


def test_captured_help_surface_supports_the_fail_verdicts():
    lines = _help_lines()
    input_format = _option_entry(lines, "--input-format")
    output_format = _option_entry(lines, "--output-format")
    assert "stream-json" in input_format and "only works with --print" in input_format
    assert "stream-json" in output_format and "only works with --print" in output_format
    # stream-json is headless-only: the SDK cannot attach to the stock TUI
    commands = _commands(lines)
    assert "mcp" in commands and "plugin|plugins" in commands
    assert "connect" not in commands and "attach" not in commands
    assert "Usage:" in lines[0]


# ---- opt-in drift probe against the installed release -------------------------


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

    version = subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, timeout=15, check=False
    )
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?: \(Claude Code\))?", version.stdout.strip())
    assert match is not None, DRIFT_MESSAGE
