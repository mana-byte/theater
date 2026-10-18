"""Fixtures shared by the native-runtime lifecycle modules."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.support.native_lifecycle import NativeFixtureHarness, RoutingIO
from theater.daemon.runtime import wiring as wiring_mod


@pytest.fixture
def native_rig(monkeypatch):
    """The native environment: shared I/O, test harness, auto gate enabled."""
    io = RoutingIO()
    harness = NativeFixtureHarness()
    monkeypatch.setattr(wiring_mod, "NATIVE_AUTO_SELECTION_ENABLED", True)
    return SimpleNamespace(io=io, harness=harness)


@pytest.fixture
def native_harness_binary(theater_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose the inert test harness executable required by public admission."""
    binary_dir = theater_home / "bin"
    binary_dir.mkdir()
    binary = binary_dir / NativeFixtureHarness.binary
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", f"{binary_dir}{os.pathsep}{os.environ.get('PATH', '')}")
