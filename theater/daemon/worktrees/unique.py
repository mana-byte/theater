"""Unique worktree creation and removal.

Each child gets ``theater/<child-id>`` at ``<repo-root>/.theater/worktrees/<child-id>``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from theater.constants.worktree import (
    GIT_QUERY_TIMEOUT_SECONDS,
    GIT_WORKTREE_ADD_TIMEOUT_SECONDS,
    GIT_WORKTREE_REMOVE_TIMEOUT_SECONDS,
    INDETERMINATE_RCS,
)
from theater.daemon.worktrees.paths import branch_name, worktree_path
from theater.daemon.worktrees.repository import _git, main_repo_root
from theater.models import BadRequest

logger = logging.getLogger("theater.worktree")


@dataclass(frozen=True, slots=True)
class WorktreeRemoveResult:
    """Outcome of a :func:`remove_worktree` call; assume nothing unless ``ok``.

    ``errors`` keeps git stderr per failed step so nobody has to re-run the commands.
    """

    ok: bool = False
    worktree_removed: bool = False
    branch_removed: bool = False
    errors: list[str] = field(default_factory=list)


def create_worktree(*, repo_root: str, child_id: str, base_branch: str | None = None) -> str:
    """Create a git worktree for a child, returning the path.

    Raises BadRequest if the path is not a git repo or the worktree already exists.
    """
    branch = branch_name(child_id)
    wt_path = worktree_path(repo_root, child_id)

    check = _git(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        expected_returncodes=(0, 1),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if check.returncode == 0:
        raise BadRequest(f"branch {branch!r} already exists")

    args = ["git", "worktree", "add", "-b", branch, wt_path]
    if base_branch:
        args.append(base_branch)

    result = _git(
        args,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_WORKTREE_ADD_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise BadRequest(
            f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}"
        )

    logger.info("created worktree for %s at %s (branch %s)", child_id, wt_path, branch)
    return wt_path


def remove_worktree(
    *,
    repo_root: str,
    child_id: str,
    delete_branch: bool = True,
) -> WorktreeRemoveResult:
    """Remove a worktree and its branch, reporting what happened; never raises on git failure.

    Killed: force and ``-D``. Exited children usually finished, so ``delete_branch=False`` keeps
    their commits. The main root is re-derived, since a worktree cwd's ``repo_root`` is itself.
    """
    from pathlib import Path

    branch = branch_name(child_id)
    result = WorktreeRemoveResult()

    # Re-derive the main root so git targets the shared admin directory.
    real_root = main_repo_root(repo_root, child_id=child_id) or repo_root
    wt_path = worktree_path(real_root, child_id)

    # --- Remove the worktree directory ---
    wt_result = _git(
        ["git", "worktree", "remove", "--force", wt_path],
        cwd=real_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_WORKTREE_REMOVE_TIMEOUT_SECONDS,
    )
    worktree_removed = wt_result.returncode == 0

    if not worktree_removed:
        # The directory may already be gone. Prune stale admin records.
        stderr = wt_result.stderr.strip()
        logger.warning("git worktree remove failed for %s: %s", child_id, stderr)
        prune_result = _git(
            ["git", "worktree", "prune", "--verbose"],
            cwd=real_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_WORKTREE_REMOVE_TIMEOUT_SECONDS,
        )
        worktree_removed = not Path(wt_path).exists()
        if not worktree_removed:
            result.errors.append(
                f"worktree remove: {stderr}"
                + (
                    f" (prune stderr: {prune_result.stderr.strip()})"
                    if prune_result.stderr.strip()
                    else ""
                )
            )

    # --- Delete the branch --- (git refuses while a worktree has it checked out)
    branch_removed = False
    if delete_branch:
        br_result = _git(
            ["git", "branch", "-D", branch],
            cwd=real_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_QUERY_TIMEOUT_SECONDS,
        )
        branch_removed = br_result.returncode == 0

        if not branch_removed:
            stderr = br_result.stderr.strip()
            verify = _git(
                ["git", "rev-parse", "--verify", branch],
                cwd=real_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=GIT_QUERY_TIMEOUT_SECONDS,
            )
            if verify.returncode == 0:
                result.errors.append(f"branch delete: {stderr}")
                logger.warning(
                    "git branch -D failed for %s (branch %s): %s",
                    child_id,
                    branch,
                    stderr,
                )
            elif verify.returncode in INDETERMINATE_RCS:
                result.errors.append(
                    f"branch delete: {stderr} (verify indeterminate, rc={verify.returncode})"
                )
                logger.warning(
                    "git branch -D for %s (branch %s): verify indeterminate (rc=%d)",
                    child_id,
                    branch,
                    verify.returncode,
                )
            else:
                branch_removed = True

    result = WorktreeRemoveResult(
        ok=worktree_removed and (branch_removed or not delete_branch),
        worktree_removed=worktree_removed,
        branch_removed=branch_removed,
        errors=result.errors,
    )

    if result.ok:
        logger.info(
            "removed worktree for %s (%s)",
            child_id,
            f"branch {branch} deleted" if delete_branch else f"branch {branch} kept",
        )
    else:
        logger.error(
            "failed to remove worktree for %s (branch %s): %s",
            child_id,
            branch,
            "; ".join(result.errors),
        )

    return result
