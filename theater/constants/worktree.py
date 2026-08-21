"""Immutable git worktree constants.

Return codes synthesized by the git helper, branch/path prefixes, and
directory layouts. These are fixed by git's CLI behavior, not user-config.
"""

from __future__ import annotations

#: git timeout — synthesized return code when a git command exceeds its deadline.
GIT_TIMEOUT_RC = 124

#: git binary missing — synthesized return code when git is not on PATH.
GIT_MISSING_RC = 127

#: git fatal error — return code for both missing refs AND fatal git errors (indeterminate).
GIT_FATAL_RC = 128

#: Return codes that do NOT prove a ref is gone; callers must exclude these.
INDETERMINATE_RCS = frozenset({GIT_TIMEOUT_RC, GIT_MISSING_RC, GIT_FATAL_RC})

#: Branch prefix for all Theater-managed worktrees.
BRANCH_PREFIX = "theater/"

#: Branch prefix for named shared worktrees.
NAMED_BRANCH_PREFIX = "theater/named/"

#: Where worktrees live relative to the repo root.
WORKTREE_DIR = ".theater/worktrees"

#: Where named worktrees live relative to the repo root.
NAMED_WORKTREE_DIR = ".theater/worktrees/named"

#: Maximum length of a named-worktree name.
MAX_NAME_LENGTH = 100
