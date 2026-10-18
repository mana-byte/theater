"""Helpers shared across a split test module."""

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
