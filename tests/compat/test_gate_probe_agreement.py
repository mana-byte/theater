"""Each adapter's gate and the probe the plugin really consults must admit the same releases."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.harness_compat.adapters import ADAPTER_NAMES, load_adapter
from scripts.harness_compat.contract import version_key
from scripts.harness_compat.probe import _probe
from tests.support.paths import REPO_ROOT


@pytest.mark.parametrize("name", ADAPTER_NAMES)
def test_the_probe_admits_the_last_gated_release_and_refuses_the_ceiling(
    name: str, tmp_path: Path
) -> None:
    adapter = load_adapter(name)
    gate = adapter.verified(REPO_ROOT)
    assert gate.ceiling is not None
    major, minor, patch = version_key(gate.ceiling)
    # A ceiling such as 2.1.0 has no previous patch release; the floor is admitted then.
    last = f"{major}.{minor}.{patch - 1}" if patch else gate.floor
    assert last is not None
    assert gate.admits(last) and not gate.admits(gate.ceiling)
    stub = tmp_path / "stub"
    assert _probe(adapter, REPO_ROOT, last, stub)[0] is True
    assert _probe(adapter, REPO_ROOT, gate.ceiling, stub)[0] is False
