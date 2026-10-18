"""Local and shipped plugin directories with a disabling helper for the plugin tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def local_dir(tmp_path) -> Path:
    """Stand-in for `$THEATER_HOME/plugins`."""
    d = tmp_path / "plugins"
    d.mkdir()
    return d
