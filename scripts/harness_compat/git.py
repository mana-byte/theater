"""The few git queries the pipeline needs: what to copy, and what a change touched."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

_TIMEOUT_SECONDS = 60


def git(root: Path, *args: str) -> str:
    """Run ``git`` in ``root``; raise RuntimeError with git's own words when it fails."""
    done = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if done.returncode:
        raise RuntimeError(f"git {' '.join(args)} failed: {done.stderr.strip()}")
    return done.stdout


#: What a tree that is not a git checkout (a code copy being copied again) never needs.
_JUNK = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"})


def _walk(root: Path) -> Iterator[str]:
    """Every file and symlink below ``root``; links are listed, never followed."""
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name not in _JUNK]
        base = Path(directory)
        for name in (*files, *(n for n in names if (base / n).is_symlink())):
            yield (base / name).relative_to(root).as_posix()


def code_files(root: Path) -> list[str]:
    """The working tree a developer sees: tracked files plus untracked ones git does not ignore.

    A code copy holds no git metadata and is already filtered, so it is walked as it is.
    """
    if not (root / ".git").exists():
        return sorted(_walk(root))
    names = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({name for name in names.split("\0") if name})


def changed_paths(root: Path) -> set[str]:
    """Every path the working tree changes against HEAD, untracked files included."""
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths: set[str] = set()
    entries = iter(status.split("\0"))
    for entry in entries:
        if len(entry) < 4:
            continue
        paths.add(entry[3:])
        if entry[0] in "RC":  # a rename lists its origin as a separate entry
            next(entries, None)
    return paths


def numstat(root: Path, path: str) -> tuple[int, int]:
    """Lines added and removed in ``path`` against HEAD."""
    line = git(root, "diff", "--numstat", "HEAD", "--", path).strip()
    if not line:
        return 0, 0
    added, removed, _ = line.split("\t", 2)
    return int(added), int(removed)


def diff_names(root: Path, base: str) -> list[str]:
    """Paths that differ between ``base`` and HEAD."""
    return [
        name for name in git(root, "diff", "--name-only", "-z", base, "HEAD").split("\0") if name
    ]


def show(root: Path, revision: str, path: str) -> str:
    """The text of ``path`` at ``revision``."""
    return git(root, "show", f"{revision}:{path}")
