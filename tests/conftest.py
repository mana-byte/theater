from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.registry import Registry
from theater.daemon.store import Store


@pytest.fixture(autouse=True)
def theater_home(monkeypatch):
    """Relocate all Theater state so tests never touch ~/.theater.

    Not pytest's `tmp_path`: its paths run to ~120 bytes, and sun_path caps a
    unix socket at 104 on macOS. Anything that binds a socket needs a short root.
    """
    root = Path(tempfile.mkdtemp(prefix="thtr-", dir="/tmp"))
    monkeypatch.setenv("THEATER_HOME", str(root))
    paths.ensure_home()
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def store(theater_home) -> Store:
    s = Store(paths.db_path())
    yield s
    s.close()


@pytest.fixture
def registry(store) -> Registry:
    return Registry(store)
