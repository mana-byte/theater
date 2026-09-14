"""Fail-closed regression pinning the Vibe single-client topology blocker.

docs/native-interaction/vibe.md owns the plan; until a stock release passes
its go criteria, the shipped plugin must stay legacy and the pinned evidence
must keep saying the attachment proof failed.
"""

from __future__ import annotations

import json
from pathlib import Path

from theater.harness.builtin.plugins.vibe.manifest import MANIFEST
from theater.harness.contracts.manifest import InterruptPlan

RECORD = Path(__file__).parent / "fixtures" / "vibe_app_server" / "topology.json"
PINNED_VERSION = "2.25.1"
PINNED_COMMIT = "2817f3df81ae05d49ba9538262edb1d5a18fa006"


def test_shipped_vibe_manifest_stays_legacy_without_a_runtime() -> None:
    assert MANIFEST.runtime is None
    assert MANIFEST.controls is not None
    assert MANIFEST.controls.interrupt == InterruptPlan(keys=("Escape",))


def test_pinned_evidence_records_no_supported_attachment() -> None:
    record = json.loads(RECORD.read_text())
    assert record["version"] == PINNED_VERSION
    assert record["commit"] == PINNED_COMMIT
    assert record["result"] == "failed: no supported stock-TUI attachment"
    assert record["goCriteria"] == {
        "observerControlRole": False,
        "stockTuiExtension": False,
        "sharedBroker": False,
    }
    assert record["observed"]["secondSessionStartSameConnection"].startswith("conflict")
    assert record["observed"]["crossProcessResumeOfLiveSession"] == "conflict: session_busy"
