"""Pin a workspace's Git state with bounded, lock-free Git commands."""

from __future__ import annotations

import os
from pathlib import Path

from theater.constants.handoff import GIT_DIRTY_OUTPUT_MAX_BYTES, GIT_PIN_TIMEOUT_SECONDS
from theater.daemon.handoff.models import GitFacts
from theater.daemon.worktrees.repository import _git

_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


def _run(argv: list[str], cwd: str):
    return _git(
        ["git", "--no-optional-locks", *argv],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_PIN_TIMEOUT_SECONDS,
        env=_ENV,
    )


def pin_git_facts(cwd: str | None) -> GitFacts:
    """Commit, branch and dirtiness now; anything Git cannot prove stays explicitly unpinned.

    The status read takes no index lock, so pinning never disturbs the source's worktree.
    """
    if not cwd or not Path(cwd).is_dir():
        return GitFacts.unpinned("workspace_missing")
    head = _run(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], cwd)
    if head.returncode != 0 or not head.stdout.strip():
        return GitFacts.unpinned("no_commit" if head.returncode == 1 else "git_unavailable")
    branch_proc = _run(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd)
    branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else None
    status = _run(["status", "--porcelain=v1", "-z", "--untracked-files=normal"], cwd)
    if status.returncode != 0:
        return GitFacts.unpinned("status_unavailable")
    entries = len([part for part in status.stdout[:GIT_DIRTY_OUTPUT_MAX_BYTES].split("\0") if part])
    return GitFacts.from_status(head.stdout.strip(), branch, entries)
