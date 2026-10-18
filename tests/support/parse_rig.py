"""Shared screen fixtures and readers for the per-harness parse tests."""

from __future__ import annotations

from pathlib import Path

from tests.support.paths import FIXTURES


def events_for(observer, path: Path):
    out = []
    for i, line in enumerate(path.read_text().splitlines()):
        out.extend(observer.parse(line, i))
    return out


SCREENS = FIXTURES / "screens"


def read_screen(name: str) -> str:
    return (SCREENS / name).read_text()
