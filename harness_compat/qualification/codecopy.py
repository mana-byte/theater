"""A disposable, independent copy of the code under test."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..git import code_files
from .cleanup import remove_tree


class UnsafeCopy(ValueError):
    """A symlink that would let the copy reach the checkout, the home or anything outside itself."""


def _check_link(root: Path, source: Path, name: str) -> Path:
    """The link's target, refused unless it is relative and stays inside ``root``."""
    target = source.readlink()
    if target.is_absolute():
        raise UnsafeCopy(f"{name} is an absolute symlink to {target}: it would escape the copy")
    resolved = source.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve()):
        raise UnsafeCopy(f"{name} points at {resolved}, outside the tree: it would escape the copy")
    return target


@contextmanager
def code_copy(root: Path) -> Iterator[Path]:
    """Copy the working tree (tracked and unignored files) to a temp dir, removed on exit.

    The copy shares nothing with ``root``: no git metadata, no build or virtualenv directories,
    so editing it (admitting one release) or running the suite in it can never touch the checkout.
    A symlink is copied only when it is relative and resolves inside the tree; any other would
    keep pointing at the checkout or the home from inside the copy, so it is refused.
    """
    base = Path(tempfile.mkdtemp(prefix="compat-code-"))
    try:
        tree = base / "code"
        for name in code_files(root):
            source = root / name
            if not (source.exists() or source.is_symlink()):
                continue  # tracked, deleted in the working tree
            target = tree / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.is_symlink():
                target.symlink_to(_check_link(root, source, name))
            elif source.is_file():
                shutil.copy2(source, target)
        yield tree
    finally:
        remove_tree(base)
