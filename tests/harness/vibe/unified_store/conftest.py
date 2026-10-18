"""Fixtures for the Vibe unified-store tests: a fresh store writer."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.vibe_store import (
    Store,
)


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    return Store(tmp_path)
