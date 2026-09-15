"""Fail-closed regression pinning the Vibe single-client topology blocker.

docs/native-interaction/vibe.md owns the plan: while every go criterion stays
false the plugin ships no runtime, and any evidence drift fails loudly so a
human reviews upstream instead of auto-wiring a runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

from theater.harness.builtin.plugins.vibe.manifest import MANIFEST
from theater.harness.contracts.manifest import InterruptPlan

RECORD = Path(__file__).parent / "fixtures" / "vibe_app_server" / "topology.json"
PINNED_VERSION = "2.25.4"
PINNED_COMMIT = "19b5b74faa78d0816b8d4d4c7d7543fc3520678c"
PINNED_ARCHIVE_DIGEST = "sha256:9af6a094e2294f11651927759020e8ee3ff88376baba21e67b8d3796acd77702"
PINNED_RESULT = "failed: no supported stock-TUI attachment"
PINNED_GO_CRITERIA = {
    "observerControlRole": False,
    "stockTuiExtension": False,
    "sharedBroker": False,
}


def test_pinned_evidence_names_the_current_inspected_release() -> None:
    record = json.loads(RECORD.read_text())
    drift = "evidence drift: inspect the latest official Vibe release"
    assert record["version"] == PINNED_VERSION, drift
    assert record["commit"] == PINNED_COMMIT, drift
    assert record["archiveDigest"] == PINNED_ARCHIVE_DIGEST
    assert record["inspectedAt"], "evidence drift: inspection date missing"
    assert record["adr"] == "docs/adr/0009-app-server-boundary.md"
    assert record["callbackOwner"] == "stock client connection"
    assert record["result"] == PINNED_RESULT
    assert record["goCriteria"] == PINNED_GO_CRITERIA
    observed = record["observed"]
    assert observed["secondSessionStartSameConnection"].startswith("conflict")
    assert observed["crossProcessResumeOfLiveSession"] == "conflict: session_busy"


def test_shipped_vibe_manifest_stays_legacy_while_topology_fails() -> None:
    record = json.loads(RECORD.read_text())
    if any(record["goCriteria"].values()):
        raise AssertionError(
            "a Vibe go criterion is true: review upstream and write a new "
            "implementation plan before wiring any runtime"
        )
    assert MANIFEST.runtime is None
    assert MANIFEST.controls is not None
    assert MANIFEST.controls.interrupt == InterruptPlan(keys=("Escape",))
