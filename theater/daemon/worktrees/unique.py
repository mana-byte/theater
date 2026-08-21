"""Unique worktree creation and removal.

Each spawned child gets a real ``git worktree`` with isolated index and HEAD.
Branch naming: ``theater/<child-id>``. The worktree path lives under
``<repo-root>/.theater/worktrees/<child-id>``.
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
    """Outcome of a :func:`remove_worktree` call.

    A caller must not assume success unless ``ok`` is ``True``. The
    ``errors`` list collects git stderr messages for each step that
    failed, so a caller (or a log reader) can diagnose without re-running
    the commands.
    """

    ok: bool = False
    worktree_removed: bool = False
    branch_removed: bool = False
    errors: list[str] = field(default_factory=list)


def create_worktree(*, repo_root: str, child_id: str, base_branch: str | None = None) -> str:
    """Create a git worktree for a child, returning the path.

    Creates a new branch ``theater/<child-id>`` from the current HEAD (or
    ``base_branch`` if given), and checks it out in a worktree at
    ``<repo>/.theater/worktrees/<child-id>``.

    Raises BadRequest if the path is not a git repo or the worktree
    already exists.
    """
    branch = branch_name(child_id)
    wt_path = worktree_path(repo_root, child_id)

    check = _git(
        ["git", "rev-parse", "--verify", branch],
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
    """Remove a worktree and its branch, reporting what actually happened.

    Called when a child is killed. Uses ``git worktree remove --force``
    so uncommitted changes are discarded (the child is dead; its
    uncommitted work is not ours to preserve). The branch is deleted
    with ``-D`` for the same reason.

    Pass ``delete_branch=False`` to prune only the directory. That is
    the right call for a child that exited on its own rather than being
    killed: it usually exited because it *finished*, and its branch is
    the only handle anyone has on the commits it made. Removing the
    directory there reclaims the disk and the worktree slot; removing
    the branch would silently destroy the result. With this flag,
    ``ok`` reflects the directory alone and ``branch_removed`` stays
    ``False`` — nothing was asked of the branch, so nothing is claimed
    about it.

    The *repo_root* argument is typically derived by the caller from
    the child's cwd via :func:`repo_root` — which, for a worktree child,
    returns the *worktree's* top level, not the main repo root. We
    re-derive the true main root here with :func:`main_repo_root` so
    that the worktree path and the branch deletion operate against the
    shared repo. If that re-derivation also fails (the cwd is gone), we
    fall back to the *repo_root* as given; the caller may have passed
    the correct value already.

    Returns a :class:`WorktreeRemoveResult`. ``ok`` is ``True`` only
    when both the worktree and the branch were removed (or were already
    gone). The function never raises on git failure — it logs git's
    stderr and returns the result, so the caller can act on it without a
    try/except around cleanup.
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
