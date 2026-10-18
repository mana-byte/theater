"""Fixtures shared by many test modules, registered as a plugin from tests/conftest.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.support.presence import AbsentPresence


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """An existing empty directory standing in for a participant's cwd."""
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


@pytest.fixture
async def absent_presence(daemon):
    """No human focus: the composed gates pass; modules opt in with ``usefixtures``."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(daemon, "presence", AbsentPresence(), raising=False)
        yield
