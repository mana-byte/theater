"""Cheap, subprocess-free worktree preflight that names the repositories a caller probably meant.

The authoritative check stays in ``worktrees.identity``; this only turns the common
"cwd is a parent of my repos" mistake into a refusal that says which cwd to pass.
"""

from __future__ import annotations

from pathlib import Path

from theater.models import BadRequest

#: Repositories named in the refusal before the rest collapse into "+N more".
SHOWN_CHILD_REPOS = 10


def _inside_git_repo(path: Path) -> bool:
    return any((candidate / ".git").exists() for candidate in (path, *path.parents))


def child_repos(path: Path) -> list[Path]:
    """Direct, non-hidden children holding ``.git`` (a file for linked worktrees), newest first."""
    found = []
    try:
        children = list(path.iterdir())
    except OSError:
        return []
    for child in children:
        if child.name.startswith(".") or not child.is_dir():
            continue
        try:
            if (child / ".git").exists():
                found.append((child.stat().st_mtime, child))
        except OSError:
            continue
    found.sort(key=lambda item: item[0], reverse=True)
    return [child for _, child in found]


def reject_worktree_outside_git(cwd: str) -> None:
    """Refuse worktree=true from a non-repository cwd, naming the repos directly under it."""
    path = Path(cwd).expanduser()
    if not path.is_dir() or _inside_git_repo(path.resolve()):
        return
    repos = child_repos(path)
    if repos:
        shown = ", ".join(repo.name for repo in repos[:SHOWN_CHILD_REPOS])
        extra = len(repos) - SHOWN_CHILD_REPOS
        more = f" (+{extra} more)" if extra > 0 else ""
        hint = f"Git repos directly under it: [{shown}]{more} - pass cwd=<one of them>"
    else:
        hint = "no Git repos directly under it - pass cwd=<a path inside a Git repository>"
    raise BadRequest(
        f"worktree=true needs cwd inside a git repository; {cwd!r} is not. "
        f"{hint}; changing harness will not help."
    )
