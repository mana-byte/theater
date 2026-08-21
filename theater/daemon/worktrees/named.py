"""Named shared worktree creation, verification, and removal.

A named worktree is an explicit expert-mode collaboration primitive.
Multiple live children spawned with the same name share one directory
and one branch — and therefore one index and one HEAD. The named
worktree's identity is persisted so a daemon restart can recognise it
and a later join can find it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from theater.constants.worktree import (
    GIT_QUERY_TIMEOUT_SECONDS,
    GIT_WORKTREE_ADD_TIMEOUT_SECONDS,
    GIT_WORKTREE_REMOVE_TIMEOUT_SECONDS,
    INDETERMINATE_RCS,
)
from theater.daemon.worktrees.paths import (
    named_branch_name,
    named_worktree_path,
    validate_name,
)
from theater.daemon.worktrees.repository import _git, main_repo_root
from theater.daemon.worktrees.unique import WorktreeRemoveResult
from theater.models import BadRequest

logger = logging.getLogger("theater.worktree")


def create_named_worktree(
    *,
    repo_root: str,
    name: str,
    base_branch: str | None = None,
) -> tuple[str, str]:
    """Create a named shared linked worktree, returning ``(path, branch)``.

    Creates a branch ``theater/named/<name>`` from ``base_branch`` (or
    current HEAD) and checks it out in
    ``<repo>/.theater/worktrees/named/<name>``. Multiple children spawned
    with the same name join this same directory and branch.

    Raises :class:`BadRequest` if the path is not a git repo, the branch
    already exists, or the worktree already exists.
    """
    validate_name(name)
    branch = named_branch_name(name)
    wt_path = named_worktree_path(repo_root, name)

    check = _git(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if check.returncode == 0:
        raise BadRequest(
            f"branch {branch!r} already exists; a named worktree "
            f"with name {name!r} was created by another spawn or a prior run"
        )

    if Path(wt_path).exists():
        raise BadRequest(
            f"worktree path {wt_path!r} already exists but the branch "
            f"{branch!r} does not; clean up the directory or pick a different name"
        )

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
            f"git worktree add failed for named worktree {name!r}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )

    logger.info("created named worktree %r at %s (branch %s)", name, wt_path, branch)
    return wt_path, branch


def verify_named_worktree(
    *,
    repo_root: str,
    name: str,
    expected_path: str,
    expected_branch: str,
) -> None:
    """Verify that a persisted named-worktree row is still intact before joining.

    Checks that:
    - the persisted path and branch equal Theater's deterministic values
    - the expected path exists as a directory
    - it is a linked worktree of the canonical main repository
    - the expected branch is checked out there

    Raises :class:`BadRequest` with an actionable message if any fact is
    stale or mismatched. Never launches a child into a missing or hijacked
    directory.
    """
    safe_path = named_worktree_path(repo_root, name)
    if Path(expected_path) != Path(safe_path):
        raise BadRequest(
            f"named worktree {name!r} has persisted path {expected_path!r}, "
            f"expected Theater-managed path {safe_path!r}; cannot join"
        )

    safe_branch = named_branch_name(name)
    if expected_branch != safe_branch:
        raise BadRequest(
            f"named worktree {name!r} has persisted branch {expected_branch!r}, "
            f"expected Theater-managed branch {safe_branch!r}; cannot join"
        )

    if not Path(expected_path).is_dir():
        raise BadRequest(
            f"named worktree {name!r} path {expected_path!r} does not exist; "
            f"the directory was removed — delete the stale named-worktree "
            f"row or pick a different name"
        )

    # Verify it is a linked worktree of the same canonical repo.
    real_root = main_repo_root(expected_path)
    if real_root is None:
        raise BadRequest(
            f"named worktree {name!r} at {expected_path!r} is not inside a "
            f"git repository; cannot join"
        )
    if Path(real_root).resolve() != Path(repo_root).resolve():
        raise BadRequest(
            f"named worktree {name!r} at {expected_path!r} belongs to a "
            f"different repository ({real_root!r}, expected {repo_root!r}); "
            f"cannot join"
        )

    # Verify the expected branch is checked out in the worktree.
    result = _git(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=expected_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise BadRequest(
            f"named worktree {name!r} at {expected_path!r}: cannot read "
            f"checked-out branch: {result.stderr.strip()}"
        )
    checked_out = result.stdout.strip()
    if checked_out != expected_branch:
        raise BadRequest(
            f"named worktree {name!r} at {expected_path!r} has branch "
            f"{checked_out!r} checked out, expected {expected_branch!r}; "
            f"the worktree was hijacked or manually switched — refusing to "
            f"join"
        )


def remove_named_worktree(
    *,
    repo_root: str,
    name: str,
    delete_branch: bool = True,
) -> WorktreeRemoveResult:
    """Remove a named shared worktree and optionally its branch.

    Like :func:`remove_worktree` but for named worktrees. The caller must
    ensure no other live participant is still using the shared directory —
    this function does not check membership, because membership is a
    daemon-level question (``Store.live_participants_in_cwd``).
    """
    branch = named_branch_name(name)
    result = WorktreeRemoveResult()

    real_root = main_repo_root(repo_root) or repo_root
    wt_path = named_worktree_path(real_root, name)

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
        stderr = wt_result.stderr.strip()
        logger.warning("git worktree remove failed for named worktree %s: %s", name, stderr)
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
                    "git branch -D failed for named worktree %s (branch %s): %s",
                    name,
                    branch,
                    stderr,
                )
            elif verify.returncode in INDETERMINATE_RCS:
                result.errors.append(
                    f"branch delete: {stderr} (verify indeterminate, rc={verify.returncode})"
                )
                logger.warning(
                    "git branch -D for named worktree %s (branch %s): delete failed "
                    "and verify was indeterminate (rc=%d); not reporting as removed",
                    name,
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
            "removed named worktree %s (%s)",
            name,
            f"branch {branch} deleted" if delete_branch else f"branch {branch} kept",
        )
    else:
        logger.error(
            "failed to remove named worktree %s (branch %s): %s",
            name,
            branch,
            "; ".join(result.errors),
        )

    return result
