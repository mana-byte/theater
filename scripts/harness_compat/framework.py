"""Orchestration. Every signature here is frozen; the framework stream implements the bodies."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .contract import Adapter, Check


def run_pytest(
    root: Path,
    nodeids: Sequence[str],
    *,
    env: Mapping[str, str],
    expected_skips: Mapping[str, str],
    timeout: float = 900.0,
) -> tuple[Check, ...]:
    """Run ``nodeids`` with pytest inside the tree at ``root``; one Check per node id.

    pass: it passed; fail: it failed; skip: it skipped and ``expected_skips`` names it;
    error: timeout, collection error, or a skip that ``expected_skips`` does not name.
    A skip or exit code 77 is never a pass.
    """
    raise NotImplementedError("framework stream")


def check_bump(adapter: Adapter, root: Path, bumped: Sequence[str]) -> list[str]:
    """Problems with a bump under ``root``; an empty list means it is coherent.

    Writes a stub binary printing ``adapter.version_line(v)`` (and ``adapter.stub_help``),
    runs the real ``adapter.probe`` against the edited tree in a subprocess, and requires
    support for every version in ``bumped``, none for the next release above, and no leftover
    string of the old limit anywhere in the changed files.
    """
    raise NotImplementedError("framework stream")
