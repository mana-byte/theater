"""Repository, tests and fixtures locations, so no test anchors on its own file's directory."""

from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"


def fixture(*parts: str) -> Path:
    """A path under ``tests/fixtures``."""
    return FIXTURES.joinpath(*parts)
