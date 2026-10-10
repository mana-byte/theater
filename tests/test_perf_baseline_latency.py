"""The perf script's observe_to_bus column, computed from stored bus rows."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
import types
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "perf_baseline.py"


@pytest.fixture
def perf(monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", types.ModuleType("psutil"))
    spec = importlib.util.spec_from_file_location("perf_baseline_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_observe_to_bus_is_bus_ts_minus_read_at_and_nan_without_samples(perf):
    with_read = json.dumps({"ts": 99.0, "read_at": 100.25})
    assert perf._latency_samples(100.5, "agent.assistant", with_read) == (1500.0, 250.0)
    legacy = json.dumps({"ts": 100.0})
    assert perf._latency_samples(100.5, "agent.assistant", legacy) == (500.0, None)
    assert perf._latency_samples(100.5, "participant.status", with_read) == (None, None)
    assert perf._latency_samples(100.5, "agent.assistant", None) == (None, None)
    assert math.isnan(perf._pct([], 0.95))


@pytest.mark.parametrize("read_at", [101.0, float("nan"), float("inf"), True, "1"])
def test_skewed_or_junk_read_at_is_skipped(perf, read_at):
    payload = json.dumps({"ts": 99.0, "read_at": read_at}, allow_nan=True)
    assert perf._latency_samples(100.0, "agent.assistant", payload)[1] is None
