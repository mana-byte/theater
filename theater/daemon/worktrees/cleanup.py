"""Explicit Git cleanup against a previously verified workspace identity."""

from __future__ import annotations

from dataclasses import dataclass

from theater.constants.worktree import (
    GIT_QUERY_TIMEOUT_SECONDS,
    GIT_TIMEOUT_RC,
    GIT_WORKTREE_REMOVE_TIMEOUT_SECONDS,
)
from theater.daemon.worktrees.identity import (
    PartialCreationInspection,
    inspect_partial_creation,
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


def cleanup_reconcile_creation(
    record: WorkspaceRecord,
    *,
    force: bool,
    delete_branch: bool,
    force_branch: bool,
) -> ExactCleanupResult:
    """Resolve a pathless creation partial only from its durable identity facts."""
    inspection = inspect_partial_creation(record)
    if inspection.path_exists:
        return cleanup_exact_worktree(
            record,
            force=force,
            delete_branch=delete_branch,
            force_branch=force_branch,
        )
    return _cleanup_pathless_creation(
        record,
        inspection,
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


def inspect_cleanup_result(record: WorkspaceRecord, *, delete_branch: bool) -> ExactCleanupResult:
    """Classify a stranded cleanup from exact path, metadata, and branch facts."""
    inspection = inspect_partial_creation(record)
    if inspection.path_exists or inspection.metadata is not None:
        return ExactCleanupResult(
            False,
            False,
            inspection.branch_head is not None,
            ("the registered worktree still exists after interrupted cleanup",),
        )
    if inspection.branch_metadata_paths:
        return ExactCleanupResult(
            False,
            False,
            inspection.branch_head is not None,
            ("the workspace branch is still claimed by worktree metadata",),
        )
    if delete_branch and inspection.branch_head is not None:
        return ExactCleanupResult(
            True,
            False,
            True,
            ("requested branch deletion did not complete",),
        )
    if not delete_branch and inspection.branch_head is None:
        return ExactCleanupResult(
            True,
            True,
            False,
            ("the branch requested for retention is missing",),
            uncertain=True,
        )
    return ExactCleanupResult(
        True,
        delete_branch,
        not delete_branch,
    )


def _cleanup_pathless_creation(
    record: WorkspaceRecord,
    inspection: PartialCreationInspection,
    *,
    delete_branch: bool,
    force_branch: bool,
) -> ExactCleanupResult:
    if force_branch and not delete_branch:
        raise ValueError("force_branch requires delete_branch")
    if record.branch is None or record.resolved_base_commit is None:
        return ExactCleanupResult(
            False,
            False,
            inspection.branch_head is not None,
            ("creation intent lacks an exact branch or base commit",),
        )
    removed_metadata = False
    if inspection.metadata is not None:
        if inspection.metadata.branch not in {None, record.branch}:
            return _partial_refusal(
                inspection,
                "stale worktree metadata names a different branch; refusing cleanup",
            )
        if any(path != inspection.path for path in inspection.branch_metadata_paths):
            return _partial_refusal(
                inspection,
                "the durable branch is still claimed by another worktree; refusing cleanup",
            )
        removed = _git(
            ["git", "worktree", "remove", "--force", inspection.path],
            cwd=inspection.canonical_repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_WORKTREE_REMOVE_TIMEOUT_SECONDS,
        )
        if removed.returncode != 0:
            detail = removed.stderr.strip() or removed.stdout.strip() or "no diagnostic"
            return ExactCleanupResult(
                False,
                False,
                inspection.branch_head is not None,
                (f"stale worktree metadata removal: {detail}",),
                uncertain=removed.returncode == GIT_TIMEOUT_RC,
            )
        removed_metadata = True
        inspection = inspect_partial_creation(record)
    if inspection.path_exists or inspection.metadata is not None:
        return _partial_refusal(
            inspection,
            "the deterministic worktree path or metadata reappeared during cleanup",
        )
    if inspection.branch_metadata_paths:
        return _partial_refusal(
            inspection,
            "the durable branch is still claimed by a worktree; refusing cleanup",
        )
    if inspection.branch_head is None:
        if removed_metadata:
            return ExactCleanupResult(True, False, False)
        return _partial_refusal(
            inspection,
            "creation has no observable Git evidence and may still be executing",
        )
    if inspection.branch_head != record.resolved_base_commit:
        return _partial_refusal(
            inspection,
            "the durable branch no longer points at the recorded creation base",
        )
    if not delete_branch:
        return ExactCleanupResult(True, False, True)
    return _delete_exact_partial_branch(
        inspection,
        branch=record.branch,
        expected_base=record.resolved_base_commit,
        force_branch=force_branch,
    )


def _partial_refusal(inspection: PartialCreationInspection, message: str) -> ExactCleanupResult:
    return ExactCleanupResult(False, False, inspection.branch_head is not None, (message,))


def _delete_exact_partial_branch(
    inspection: PartialCreationInspection,
    *,
    branch: str,
    expected_base: str,
    force_branch: bool,
) -> ExactCleanupResult:
    assert inspection.metadata is None
    assert not inspection.branch_metadata_paths
    if not force_branch:
        merged = _git(
            ["git", "merge-base", "--is-ancestor", f"refs/heads/{branch}", "HEAD"],
            cwd=inspection.canonical_repository_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_QUERY_TIMEOUT_SECONDS,
        )
        if merged.returncode == 1:
            return ExactCleanupResult(
                True,
                False,
                True,
                ("branch delete: branch is not fully merged; use force_branch=true",),
            )
        if merged.returncode != 0:
            detail = merged.stderr.strip() or merged.stdout.strip() or "no diagnostic"
            return ExactCleanupResult(
                False,
                False,
                True,
                (f"branch merge check: {detail}",),
                uncertain=merged.returncode == GIT_TIMEOUT_RC,
            )
    deleted = _git(
        ["git", "update-ref", "-d", f"refs/heads/{branch}", expected_base],
        cwd=inspection.canonical_repository_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if deleted.returncode == 0:
        return ExactCleanupResult(True, True, False)
    if deleted.returncode == GIT_TIMEOUT_RC:
        detail = deleted.stderr.strip() or "Git timed out"
        return ExactCleanupResult(False, False, True, (f"branch delete: {detail}",), uncertain=True)
    try:
        after = _branch_head(inspection.canonical_repository_root, branch)
    except RuntimeError as exc:
        return ExactCleanupResult(False, False, True, (str(exc),), uncertain=True)
    if after is None:
        return ExactCleanupResult(True, True, False)
    if after != expected_base:
        return ExactCleanupResult(
            False,
            False,
            True,
            ("creation branch moved while cleanup was fenced; refusing deletion",),
        )
    detail = deleted.stderr.strip() or deleted.stdout.strip() or "no diagnostic"
    return ExactCleanupResult(True, False, True, (f"branch delete: {detail}",))


def _branch_head(root: str, branch: str) -> str | None:
    result = _git(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return None
    detail = result.stderr.strip() or "no diagnostic"
    raise RuntimeError(
        f"could not inspect creation branch; Git returned {result.returncode}: {detail}"
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


__all__ = [
    "ExactCleanupResult",
    "cleanup_exact_worktree",
    "cleanup_reconcile_creation",
    "cleanup_retained_branch",
    "inspect_cleanup_result",
]
