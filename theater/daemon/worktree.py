"""Compatibility façade; production code imports ``theater.daemon.worktrees`` directly."""

from __future__ import annotations

import subprocess  # tests monkeypatch worktree.subprocess.run

from theater.constants.worktree import (
    BRANCH_PREFIX,
    GIT_FATAL_RC,
    GIT_MISSING_RC,
    GIT_TIMEOUT_RC,
    INDETERMINATE_RCS,
    NAMED_BRANCH_PREFIX,
    NAMED_WORKTREE_DIR,
    WORKTREE_DIR,
)
from theater.daemon.worktrees import (
    WorktreeRemoveResult,
    _git,
    branch_name,
    create_named_worktree,
    create_worktree,
    is_git_repo,
    main_repo_root,
    named_branch_name,
    named_worktree_path,
    remove_named_worktree,
    remove_worktree,
    repo_root,
    validate_name,
    verify_named_worktree,
    worktree_path,
)

# Old private name used by callers that branch on indeterminate return codes.
_INDETERMINATE_RCS = INDETERMINATE_RCS

__all__ = [
    "BRANCH_PREFIX",
    "GIT_FATAL_RC",
    "GIT_MISSING_RC",
    "GIT_TIMEOUT_RC",
    "INDETERMINATE_RCS",
    "NAMED_BRANCH_PREFIX",
    "NAMED_WORKTREE_DIR",
    "WORKTREE_DIR",
    "_INDETERMINATE_RCS",
    "WorktreeRemoveResult",
    "_git",
    "branch_name",
    "create_named_worktree",
    "create_worktree",
    "is_git_repo",
    "main_repo_root",
    "named_branch_name",
    "named_worktree_path",
    "remove_named_worktree",
    "remove_worktree",
    "repo_root",
    "subprocess",
    "validate_name",
    "verify_named_worktree",
    "worktree_path",
]
