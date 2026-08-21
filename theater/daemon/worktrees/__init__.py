"""Git worktree management for spawned children.

Public API re-exports the functions, constants, and dataclass that callers
and the compatibility façade consume. Internal modules own cohesive
responsibilities: path derivation, git invocation, unique worktrees, and
named worktrees.
"""

from __future__ import annotations

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
from theater.daemon.worktrees.named import (
    create_named_worktree,
    remove_named_worktree,
    verify_named_worktree,
)
from theater.daemon.worktrees.paths import (
    branch_name,
    named_branch_name,
    named_worktree_path,
    validate_name,
    worktree_path,
)
from theater.daemon.worktrees.repository import (
    _git,
    is_git_repo,
    main_repo_root,
    repo_root,
)
from theater.daemon.worktrees.unique import (
    WorktreeRemoveResult,
    create_worktree,
    remove_worktree,
)

__all__ = [
    "BRANCH_PREFIX",
    "GIT_FATAL_RC",
    "GIT_MISSING_RC",
    "GIT_TIMEOUT_RC",
    "INDETERMINATE_RCS",
    "NAMED_BRANCH_PREFIX",
    "NAMED_WORKTREE_DIR",
    "WORKTREE_DIR",
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
    "validate_name",
    "verify_named_worktree",
    "worktree_path",
]
