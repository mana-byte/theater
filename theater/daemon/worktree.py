"""Git worktree management for spawned children.

Each spawned child gets a real `git worktree` — isolated index and HEAD.
The spec (§11) is explicit: one shared worktree with children confined to
subfolders is rejected because git index/HEAD are worktree-global and
concurrent staging corrupts each other.

Branch naming: `theater/<child-id>` (e.g. `theater/a3f2c1b9d4e8`). The
child-id is unique, so the branch name is too.

The worktree path lives under `<repo-root>/.theater/worktrees/<child-id>`.
This keeps worktrees close to the repo (so relative paths and tooling
work) but out of the way of normal git operations. The `.theater`
directory is already in `.gitignore` (or should be).

Merge-back: the child commits to its own branch and reports the branch
name in its result. The parent decides. Auto-merge would mean the
daemon silently making integration decisions on unreviewed code; handing
back a branch name keeps the merge an explicit act that shows up on the
bus.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from theater.models import BadRequest

logger = logging.getLogger("theater.worktree")

#: Branch prefix for all Theater-managed worktrees.
BRANCH_PREFIX = "theater/"

#: Where worktrees live relative to the repo root.
WORKTREE_DIR = ".theater/worktrees"


def branch_name(child_id: str) -> str:
    """The git branch name for a Theater child."""
    return f"{BRANCH_PREFIX}{child_id}"


def worktree_path(repo_root: str, child_id: str) -> str:
    """The filesystem path where a child's worktree lives."""
    return str(Path(repo_root) / WORKTREE_DIR / child_id)


def is_git_repo(path: str) -> bool:
    """Is `path` inside a git repo?"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def repo_root(path: str) -> str | None:
    """The top-level directory of the git repo containing `path`."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (OSError, subprocess.SubprocessError):
        return None


def create_worktree(
    *, repo_root: str, child_id: str, base_branch: str | None = None
) -> str:
    """Create a git worktree for a child, returning the path.

    Creates a new branch `theater/<child-id>` from the current HEAD (or
    `base_branch` if given), and checks it out in a worktree at
    `<repo>/.theater/worktrees/<child-id>`.

    Raises BadRequest if the path is not a git repo or the worktree
    already exists.
    """
    branch = branch_name(child_id)
    wt_path = worktree_path(repo_root, child_id)

    # Check if the branch already exists.
    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if check.returncode == 0:
        raise BadRequest(f"branch {branch!r} already exists")

    # Create the worktree with a new branch.
    args = ["git", "worktree", "add", "-b", branch, wt_path]
    if base_branch:
        args.append(base_branch)

    result = subprocess.run(
        args,
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise BadRequest(
            f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}"
        )

    logger.info("created worktree for %s at %s (branch %s)", child_id, wt_path, branch)
    return wt_path


def remove_worktree(*, repo_root: str, child_id: str) -> None:
    """Remove a worktree and its branch.

    Called when a child is killed. Uses `git worktree remove --force`
    so uncommitted changes are discarded (the child is dead; its
    uncommitted work is not ours to preserve). The branch is deleted
    with `-D` for the same reason.
    """
    branch = branch_name(child_id)
    wt_path = worktree_path(repo_root, child_id)

    # Remove the worktree directory.
    subprocess.run(
        ["git", "worktree", "remove", "--force", wt_path],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=10,
    )

    # Delete the branch.
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=5,
    )

    logger.info("removed worktree for %s (branch %s)", child_id, branch)
