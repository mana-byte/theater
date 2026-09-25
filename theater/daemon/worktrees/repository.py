"""Git invocation and repo/main-root discovery.

Discovery distinguishes a linked worktree's own top level from the shared main root.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from theater import timing
from theater.constants.worktree import (
    GIT_MISSING_RC,
    GIT_QUERY_TIMEOUT_SECONDS,
    GIT_TIMEOUT_RC,
    WORKTREE_DIR,
)
from theater.observability.catalog import GIT_COMMAND

logger = logging.getLogger("theater.worktree")


def _git(
    argv: list[str], *, expected_returncodes: tuple[int, ...] = (0,), **kwargs
) -> subprocess.CompletedProcess:
    """Run and time a git command; never raises on timeout or missing binary.

    Synthesizes a failed ``CompletedProcess`` (rc 124/127) so callers just branch on
    returncode; encoding pinned to match ``tmux/client.py``.
    """
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "backslashreplace")
    command = "-".join(argv[1:3])
    with timing.span(GIT_COMMAND, command=command, cwd=kwargs.get("cwd")) as sp:
        try:
            proc = subprocess.run(argv, **kwargs)  # noqa: PLW1510  (callers pass check=)
        except subprocess.TimeoutExpired as exc:
            sp["rc"] = GIT_TIMEOUT_RC
            sp.set_result("error", error_type="git_timeout")
            return subprocess.CompletedProcess(
                args=argv,
                returncode=GIT_TIMEOUT_RC,
                stdout="",
                stderr=f"git timed out after {exc.timeout}s",
            )
        except OSError as exc:
            sp["rc"] = GIT_MISSING_RC
            sp.set_result("error", error_type="git_missing")
            return subprocess.CompletedProcess(
                args=argv, returncode=GIT_MISSING_RC, stdout="", stderr=str(exc)
            )
        sp["rc"] = proc.returncode
        if proc.returncode not in expected_returncodes:
            sp.set_result("error", error_type="git_error")
        return proc


def is_git_repo(path: str) -> bool:
    """Is `path` inside a git repo?"""
    try:
        result = _git(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    else:
        return result.returncode == 0


def repo_root(path: str) -> str | None:
    """The top-level directory of the git repo containing `path`.

    Inside a linked worktree this is the worktree's own top; use :func:`main_repo_root`.
    """
    try:
        result = _git(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    else:
        return result.stdout.strip() if result.returncode == 0 else None


def main_repo_root(path: str, child_id: str | None = None) -> str | None:
    """The main repo root for any path inside the repo or a linked worktree.

    Uses ``--git-common-dir`` since ``--show-toplevel`` gives the worktree's top. If *path* is
    gone, fall back to stripping the ``.theater/worktrees/<child_id>`` suffix we guarantee.
    """
    try:
        result = _git(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        result = None

    if result is not None and result.returncode == 0:
        common_dir = result.stdout.strip()
        if common_dir:
            git_dir = Path(common_dir)
            return str(git_dir.parent.resolve())

    # Fallback: the directory is gone, so git can't help. Strip the worktree suffix.
    if child_id is not None:
        suffix = f"/{WORKTREE_DIR}/{child_id}"
        if path.endswith(suffix):
            return path[: -len(suffix)]

    return None
