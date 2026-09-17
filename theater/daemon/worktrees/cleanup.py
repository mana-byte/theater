"""Explicit Git cleanup against a previously verified workspace identity."""

from __future__ import annotations

from dataclasses import dataclass

from theater.constants.worktree import (
    GIT_QUERY_TIMEOUT_SECONDS,
    GIT_TIMEOUT_RC,
    GIT_WORKTREE_REMOVE_TIMEOUT_SECONDS,
)
from theater.daemon.worktrees.identity import (
    inspect_registered_worktree,
    validate_canonical_repository,
)
from theater.daemon.worktrees.repository import _git
from theater.models import WorkspaceRecord


@dataclass(frozen=True, slots=True)
class ExactCleanupResult:
    worktree_removed: bool
    branch_removed: bool
    branch_retained: bool
    errors: tuple[str, ...] = ()
    uncertain: bool = False

    @property
    def ok(self) -> bool:
        return self.worktree_removed and not self.errors

    def to_wire(self) -> dict[str, object]:
        return {
            "worktree_removed": self.worktree_removed,
            "branch_removed": self.branch_removed,
            "branch_retained": self.branch_retained,
            "errors": list(self.errors),
            "uncertain": self.uncertain,
        }


def cleanup_exact_worktree(
    record: WorkspaceRecord,
    *,
    force: bool,
    delete_branch: bool,
    force_branch: bool,
) -> ExactCleanupResult:
    if force_branch and not delete_branch:
        raise ValueError("force_branch requires delete_branch")
    inspection = inspect_registered_worktree(record)
    args = ["git", "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(inspection.path)
    removed = _git(
        args,
        cwd=inspection.canonical_repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_WORKTREE_REMOVE_TIMEOUT_SECONDS,
    )
    if removed.returncode != 0:
        dirty = "; workspace was dirty" if inspection.dirty else ""
        detail = removed.stderr.strip() or removed.stdout.strip() or "no diagnostic"
        return ExactCleanupResult(
            False,
            False,
            True,
            (f"worktree remove: {detail}{dirty}",),
            uncertain=removed.returncode == GIT_TIMEOUT_RC,
        )
    return _finish_branch(
        inspection.canonical_repository_root,
        inspection.branch,
        delete_branch=delete_branch,
        force_branch=force_branch,
    )


def cleanup_retained_branch(
    record: WorkspaceRecord, *, delete_branch: bool, force_branch: bool
) -> ExactCleanupResult:
    if record.canonical_repository_root is None or record.branch is None:
        raise ValueError("a retained workspace branch requires stored repository facts")
    if force_branch and not delete_branch:
        raise ValueError("force_branch requires delete_branch")
    root = validate_canonical_repository(record.canonical_repository_root)
    return _finish_branch(
        root,
        record.branch,
        delete_branch=delete_branch,
        force_branch=force_branch,
    )


def _finish_branch(
    root: str, branch: str, *, delete_branch: bool, force_branch: bool
) -> ExactCleanupResult:
    if not delete_branch:
        try:
            retained = _branch_exists(root, branch)
        except RuntimeError as exc:
            return ExactCleanupResult(True, False, True, (str(exc),), uncertain=True)
        return ExactCleanupResult(True, False, retained)
    flag = "-D" if force_branch else "-d"
    deleted = _git(
        ["git", "branch", flag, "--", branch],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    try:
        retained = _branch_exists(root, branch)
    except RuntimeError as exc:
        return ExactCleanupResult(True, False, True, (str(exc),), uncertain=True)
    if not retained:
        return ExactCleanupResult(True, True, False)
    detail = deleted.stderr.strip() or deleted.stdout.strip() or "no diagnostic"
    return ExactCleanupResult(True, False, retained, (f"branch delete: {detail}",))


def _branch_exists(root: str, branch: str) -> bool:
    result = _git(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or "no diagnostic"
    raise RuntimeError(
        f"could not verify retained branch; Git returned {result.returncode}: {detail}"
    )


__all__ = ["ExactCleanupResult", "cleanup_exact_worktree", "cleanup_retained_branch"]
