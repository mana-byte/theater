"""Helpers shared across a split test module."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.vibe_store import (
    Store,
)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)
