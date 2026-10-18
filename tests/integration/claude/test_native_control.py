"""Opt-in stock-claude run of the native-control messaging conformance."""

from __future__ import annotations

import os
import shutil

import pytest

from tests.native import claude_messaging_client as harness
from tests.support.claude_native_control import RECORD

pytestmark = pytest.mark.integration

PROOF_ENV = harness.PROOF_ENV
PROBE_ENABLED = os.environ.get(PROOF_ENV) == "1" and bool(shutil.which("claude"))


@pytest.mark.skipif(not PROBE_ENABLED, reason=f"set {PROOF_ENV}=1 with a stock claude")
def test_live_conformance_writes_a_fresh_record_outside_the_fixture_tree(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    facts = harness.binary_facts()
    if facts is None or not facts.qualified:
        pytest.skip(f"no qualified stock binary at the {harness.SAME_MACHINE_FLOOR} floor")
        return
    before = RECORD.read_bytes()
    out = tmp_path_factory.mktemp("claude-proof") / "messaging_conformance.json"
    record = harness.run_conformance(facts.resolved, out_path=out)
    assert record["result"].startswith(harness.PASS_RESULT_PREFIX)
    assert out.exists()
    assert RECORD.read_bytes() == before, "fixture updates are a separate reviewed action"
