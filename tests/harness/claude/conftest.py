"""Fixtures for the Claude harness tests: the transcript root and an observer over it."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.shipped import ClaudeCodeObserver


@pytest.fixture
def root(tmp_path) -> Path:
    d = tmp_path / "projects"
    d.mkdir()
    return d


@pytest.fixture
def observer(root) -> ClaudeCodeObserver:
    return ClaudeCodeObserver(root=root)
