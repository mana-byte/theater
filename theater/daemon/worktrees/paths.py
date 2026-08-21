"""Branch/path derivation and name validation for Theater worktrees.

Owns the deterministic relationship between a child id or name and the
git branch and filesystem path it maps to, plus the name validation
that gates named worktrees before they enter a path or ref.
"""

from __future__ import annotations

import re
from pathlib import Path

from theater.constants.worktree import (
    BRANCH_PREFIX,
    GIT_QUERY_TIMEOUT_SECONDS,
    MAX_NAME_LENGTH,
    NAMED_BRANCH_PREFIX,
    NAMED_WORKTREE_DIR,
    WORKTREE_DIR,
)
from theater.models import BadRequest

#: A git ref-name component must start alphanumeric; we reject '/' for single-component names.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: Names that look like CLI options are dangerous in paths and refs.
_OPTION_LIKE = re.compile(r"^-")

#: Reserved names that must not be used as worktree names.
_RESERVED_NAMES = frozenset({"head", "master", "main", "."})


def branch_name(child_id: str) -> str:
    """The git branch name for a Theater child."""
    return f"{BRANCH_PREFIX}{child_id}"


def named_branch_name(name: str) -> str:
    """The git branch name for a named shared worktree."""
    return f"{NAMED_BRANCH_PREFIX}{name}"


def worktree_path(repo_root: str, child_id: str) -> str:
    """The filesystem path where a child's worktree lives."""
    return str(Path(repo_root) / WORKTREE_DIR / child_id)


def named_worktree_path(repo_root: str, name: str) -> str:
    """The filesystem path where a named shared worktree lives."""
    return str(Path(repo_root) / NAMED_WORKTREE_DIR / name)


def validate_name(name: str) -> None:
    """Validate a named-worktree name before using it in a path or ref.

    Rejects empty strings, traversal (``..``, ``/``), option-like names,
    reserved names, and names that are not valid single-component git refs.
    The final branch is validated with ``git check-ref-format`` so that
    trailing dots, repeated ``..``, ``.lock`` suffixes, and other ref-format
    violations are caught. Raises :class:`BadRequest` with an actionable message.
    """
    from theater.daemon.worktrees.repository import _git

    if not isinstance(name, str) or not name.strip():
        raise BadRequest("worktree name must be a non-empty string")
    if _OPTION_LIKE.match(name):
        raise BadRequest(
            f"worktree name {name!r} must not start with '-' (looks like a CLI option)"
        )
    if "/" in name:
        raise BadRequest(
            f"worktree name {name!r} must not contain '/' (use a single path component)"
        )
    if name in ("..", "."):
        raise BadRequest(f"worktree name {name!r} is a reserved path component")
    if not _NAME_RE.match(name):
        raise BadRequest(
            f"worktree name {name!r} is not a valid git ref component: "
            f"must start with an alphanumeric and may contain only "
            f"alphanumerics, '-', '_', and '.'"
        )
    if name.lower() in _RESERVED_NAMES:
        raise BadRequest(f"worktree name {name!r} is reserved")
    if len(name) > MAX_NAME_LENGTH:
        raise BadRequest(f"worktree name {name!r} is too long (max 100 characters)")
    # Validate the full branch ref with git check-ref-format.
    branch = named_branch_name(name)
    result = _git(
        ["git", "check-ref-format", "--branch", branch],
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise BadRequest(
            f"worktree name {name!r} produces an invalid git ref {branch!r}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
