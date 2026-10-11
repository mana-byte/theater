"""Pin a workspace's Git state with one bounded, lock-free Git command."""

from __future__ import annotations

import os
from pathlib import Path

from theater.constants.handoff import GIT_DIRTY_OUTPUT_MAX_BYTES, GIT_PIN_TIMEOUT_SECONDS
from theater.daemon.handoff.models import GitFacts
from theater.daemon.worktrees.repository import _git

_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
#: Record kinds of ``status --porcelain=v2`` that are changed, renamed, unmerged or untracked paths.
_ENTRY_PREFIXES = ("1 ", "2 ", "u ", "? ")


def pin_git_facts(cwd: str | None) -> GitFacts:
    """Commit, branch and dirtiness now, from a single ``git status``.

    Anything Git cannot prove stays explicitly unpinned. The read takes no index lock, so
    pinning never disturbs the source's worktree, and one process answers all three questions.
    """
    if not cwd or not Path(cwd).is_dir():
        return GitFacts.unpinned("workspace_missing")
    done = _git(
        [
            "git",
            "--no-optional-locks",
            "status",
            "--porcelain=v2",
            "--branch",
            "-z",
            "--untracked-files=normal",
        ],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_PIN_TIMEOUT_SECONDS,
        env=_ENV,
    )
    if done.returncode != 0:
        return GitFacts.unpinned("git_unavailable")
    oid, head, entries = None, None, 0
    for record in done.stdout[:GIT_DIRTY_OUTPUT_MAX_BYTES].split("\0"):
        if record.startswith("# branch.oid "):
            oid = record.split(" ", 2)[2]
        elif record.startswith("# branch.head "):
            head = record.split(" ", 2)[2]
        elif record.startswith(_ENTRY_PREFIXES):
            entries += 1
    if oid is None or oid == "(initial)":
        return GitFacts.unpinned("no_commit")
    return GitFacts.from_status(oid, None if head in (None, "(detached)") else head, entries)
