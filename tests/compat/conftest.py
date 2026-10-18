"""Fixtures for the harness-compat pipeline tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def isolated_evidence_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the evidence cache at a per-test directory instead of the user's cache."""
    cache = tmp_path.parent / f"{tmp_path.name}-cache"
    monkeypatch.setenv("HARNESS_COMPAT_CACHE", str(cache))
    yield
    shutil.rmtree(cache, ignore_errors=True)
