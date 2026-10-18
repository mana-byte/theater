"""Throwaway git repositories for tests that only need a repo root."""

from __future__ import annotations

import subprocess
from pathlib import Path


def init_repo(tmp_path: Path, name: str) -> Path:
    """Create an empty git repository directory named ``name`` under ``tmp_path``."""
    path = tmp_path / name
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    return path
