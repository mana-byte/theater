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


def committed_repo(tmp_path: Path) -> str:
    """A real git repository on ``main`` with one commit; returns its root path."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("# test repo\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return str(root)
