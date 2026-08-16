"""Git worktree management for spawned children.

Each spawned child gets a real `git worktree` — isolated index and HEAD.
The spec (§11) is explicit: one shared worktree with children confined to
subfolders is rejected because git index/HEAD are worktree-global and
concurrent staging corrupts each other.

Branch naming: `theater/<child-id>` (e.g. `theater/a3f2c1b9d4e8`). The
child-id is unique, so the branch name is too.

The worktree path lives under `<repo-root>/.theater/worktrees/<child-id>`.
This keeps worktrees close to the repo (so relative paths and tooling
work) but out of the way of normal git operations. The `.theater`
directory is already in `.gitignore` (or should be).

Merge-back: the child commits to its own branch and reports the branch
name in its result. The parent decides. Auto-merge would mean the
daemon silently making integration decisions on unreviewed code; handing
back a branch name keeps the merge an explicit act that shows up on the
bus.

A *named* worktree (``worktree="my-name"``) is an explicit expert-mode
collaboration primitive, not filesystem or Git isolation. Multiple live
children spawned with the same name in the same canonical main repository
share one directory and one branch — and therefore one index and one HEAD.
Concurrent ``git add``/``commit`` operations can interfere, and the KV
store does not make file claims atomic or enforce ownership. The named
worktree's identity is persisted in the ``named_worktrees`` table so a
daemon restart can recognise it and a later join can find it.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from theater.models import BadRequest

logger = logging.getLogger("theater.worktree")

#: Branch prefix for all Theater-managed worktrees.
BRANCH_PREFIX = "theater/"

#: Branch prefix for named shared worktrees.
NAMED_BRANCH_PREFIX = "theater/named/"

#: Where worktrees live relative to the repo root.
WORKTREE_DIR = ".theater/worktrees"

#: Where named worktrees live relative to the repo root.
NAMED_WORKTREE_DIR = ".theater/worktrees/named"

#: A git ref-name component (after ``refs/heads/``) must start with an
#: alphanumeric and may contain alphanumerics, ``-``, ``_``, ``.``, and ``/``.
#: We additionally reject ``/`` to keep the name a single path component.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: Names that look like CLI options would be dangerous in paths and refs.
_OPTION_LIKE = re.compile(r"^-")

#: Reserved names that must not be used as worktree names.
_RESERVED_NAMES = frozenset({"head", "master", "main", "."})


def branch_name(child_id: str) -> str:
    """The git branch name for a Theater child."""
    return f"{BRANCH_PREFIX}{child_id}"


def named_branch_name(name: str) -> str:
    """The git branch name for a named shared worktree."""
    return f"{NAMED_BRANCH_PREFIX}{name}"


def worktree_path(repo_root: str, child_id: str) -> str:
    """The filesystem path where a child's worktree lives."""
    return str(Path(repo_root) / WORKTREE_DIR / child_id)


def named_worktree_path(repo_root: str, name: str) -> str:
    """The filesystem path where a named shared worktree lives."""
    return str(Path(repo_root) / NAMED_WORKTREE_DIR / name)


def validate_name(name: str) -> None:
    """Validate a named-worktree name before using it in a path or ref.

    Rejects empty strings, traversal (``..``, ``/``), option-like names
    (``-foo``), and names that are not valid single-component git refs.
    In addition to the single-safe-path-component checks, the final branch
    is validated with ``git check-ref-format`` so that trailing dots,
    repeated ``..``, ``.lock`` suffixes, and other ref-format violations
    are caught. Raises :class:`BadRequest` with an actionable message.
    """
    if not isinstance(name, str) or not name.strip():
        raise BadRequest("worktree name must be a non-empty string")
    if _OPTION_LIKE.match(name):
        raise BadRequest(
            f"worktree name {name!r} must not start with '-' (looks like a CLI option)"
        )
    if "/" in name:
        raise BadRequest(
            f"worktree name {name!r} must not contain '/' (use a single path component)"
        )
    if name in ("..", "."):
        raise BadRequest(f"worktree name {name!r} is a reserved path component")
    if not _NAME_RE.match(name):
        raise BadRequest(
            f"worktree name {name!r} is not a valid git ref component: "
            f"must start with an alphanumeric and may contain only "
            f"alphanumerics, '-', '_', and '.'"
        )
    if name.lower() in _RESERVED_NAMES:
        raise BadRequest(f"worktree name {name!r} is reserved")
    if len(name) > 100:
        raise BadRequest(f"worktree name {name!r} is too long (max 100 characters)")
    # Validate the full branch ref with git check-ref-format. This catches
    # trailing dots, repeated '..', '.lock' suffixes, and other ref-format
    # rules the regex above cannot enforce.
    branch = named_branch_name(name)
    result = subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode != 0:
        raise BadRequest(
            f"worktree name {name!r} produces an invalid git ref {branch!r}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def is_git_repo(path: str) -> bool:
    """Is `path` inside a git repo?"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    else:
        return result.returncode == 0


def repo_root(path: str) -> str | None:
    """The top-level directory of the git repo containing `path`.

    Beware: for a path inside a *linked worktree*, git returns the
    worktree's own top level, not the main repo's. Use
    :func:`main_repo_root` when you need the shared root.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    else:
        return result.stdout.strip() if result.returncode == 0 else None


def main_repo_root(path: str, child_id: str | None = None) -> str | None:
    """The main repo root for any path inside the repo or a linked worktree.

    ``git rev-parse --path-format=absolute --git-common-dir`` returns
    ``<main-repo>/.git`` — the *shared* admin directory — whose parent is
    the main repo root. This works from inside a linked worktree, where
    ``--show-toplevel`` would return the worktree's own top level instead.

    If the directory at *path* no longer exists (the worktree was
    deleted out from under us, or the cwd is stale), the git call fails.
    In that case we fall back to stripping the known
    ``.theater/worktrees/<child_id>`` suffix that :func:`create_worktree`
    guarantees, provided *child_id* is given.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=path,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        result = None

    if result is not None and result.returncode == 0:
        common_dir = result.stdout.strip()
        if common_dir:
            git_dir = Path(common_dir)
            # common_dir is <main-repo>/.git; its parent is the main root.
            return str(git_dir.parent.resolve())

    # Fallback: the directory is gone, so git can't help. Strip the
    # worktree suffix that create_worktree appends.
    if child_id is not None:
        suffix = f"/{WORKTREE_DIR}/{child_id}"
        if path.endswith(suffix):
            return path[: -len(suffix)]

    return None


@dataclass(frozen=True, slots=True)
class WorktreeRemoveResult:
    """Outcome of a :func:`remove_worktree` call.

    A caller must not assume success unless ``ok`` is ``True``. The
    ``errors`` list collects git stderr messages for each step that
    failed, so a caller (or a log reader) can diagnose without re-running
    the commands.
    """

    ok: bool = False
    worktree_removed: bool = False
    branch_removed: bool = False
    errors: list[str] = field(default_factory=list)


def create_worktree(*, repo_root: str, child_id: str, base_branch: str | None = None) -> str:
    """Create a git worktree for a child, returning the path.

    Creates a new branch `theater/<child-id>` from the current HEAD (or
    `base_branch` if given), and checks it out in a worktree at
    `<repo>/.theater/worktrees/<child-id>`.

    Raises BadRequest if the path is not a git repo or the worktree
    already exists.
    """
    branch = branch_name(child_id)
    wt_path = worktree_path(repo_root, child_id)

    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if check.returncode == 0:
        raise BadRequest(f"branch {branch!r} already exists")

    args = ["git", "worktree", "add", "-b", branch, wt_path]
    if base_branch:
        args.append(base_branch)

    result = subprocess.run(
        args,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise BadRequest(
            f"git worktree add failed: {result.stderr.strip() or result.stdout.strip()}"
        )

    logger.info("created worktree for %s at %s (branch %s)", child_id, wt_path, branch)
    return wt_path


def remove_worktree(
    *,
    repo_root: str,
    child_id: str,
    delete_branch: bool = True,
) -> WorktreeRemoveResult:
    """Remove a worktree and its branch, reporting what actually happened.

    Called when a child is killed. Uses ``git worktree remove --force``
    so uncommitted changes are discarded (the child is dead; its
    uncommitted work is not ours to preserve). The branch is deleted
    with ``-D`` for the same reason.

    Pass ``delete_branch=False`` to prune only the directory. That is
    the right call for a child that exited on its own rather than being
    killed: it usually exited because it *finished*, and its branch is
    the only handle anyone has on the commits it made. Removing the
    directory there reclaims the disk and the worktree slot; removing
    the branch would silently destroy the result. With this flag,
    ``ok`` reflects the directory alone and ``branch_removed`` stays
    ``False`` — nothing was asked of the branch, so nothing is claimed
    about it.

    The *repo_root* argument is typically derived by the caller from
    the child's cwd via :func:`repo_root` — which, for a worktree child,
    returns the *worktree's* top level, not the main repo root. We
    re-derive the true main root here with :func:`main_repo_root` so
    that the worktree path and the branch deletion operate against the
    shared repo. If that re-derivation also fails (the cwd is gone), we
    fall back to the *repo_root* as given; the caller may have passed
    the correct value already.

    Returns a :class:`WorktreeRemoveResult`. ``ok`` is ``True`` only
    when both the worktree and the branch were removed (or were already
    gone). The function never raises on git failure — it logs git's
    stderr and returns the result, so the caller can act on it without a
    try/except around cleanup.
    """
    branch = branch_name(child_id)
    result = WorktreeRemoveResult()

    # Re-derive the main root so git worktree commands target the
    # shared admin directory. Pass child_id for the fallback.
    real_root = main_repo_root(repo_root, child_id=child_id) or repo_root
    wt_path = worktree_path(real_root, child_id)

    # --- Remove the worktree directory -----------------------------------
    #
    # git worktree remove can fail if the directory was already deleted,
    # in which case we prune the stale admin record and continue to
    # branch deletion. A genuine git error (corrupt metadata) means
    # pruning won't help, but we still try branch deletion.
    wt_result = subprocess.run(
        ["git", "worktree", "remove", "--force", wt_path],
        cwd=real_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    worktree_removed = wt_result.returncode == 0

    if not worktree_removed:
        # The directory may already be gone. Prune stale admin records
        # so the branch is not "checked out" in a dead worktree.
        stderr = wt_result.stderr.strip()
        logger.warning("git worktree remove failed for %s: %s", child_id, stderr)
        prune_result = subprocess.run(
            ["git", "worktree", "prune", "--verbose"],
            cwd=real_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
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

    # --- Delete the branch -----------------------------------------------
    #
    # git refuses to delete a branch a worktree still has checked out,
    # so this comes after worktree removal. If the branch is already
    # gone, that is the desired end state.
    branch_removed = False
    if delete_branch:
        br_result = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=real_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch_removed = br_result.returncode == 0

        if not branch_removed:
            stderr = br_result.stderr.strip()
            # git branch -D returns nonzero if the branch doesn't exist.
            # If rev-parse can't find it, it's already gone — success.
            verify = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=real_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if verify.returncode != 0:
                branch_removed = True
            else:
                result.errors.append(f"branch delete: {stderr}")
                logger.warning(
                    "git branch -D failed for %s (branch %s): %s",
                    child_id,
                    branch,
                    stderr,
                )

    result = WorktreeRemoveResult(
        ok=worktree_removed and (branch_removed or not delete_branch),
        worktree_removed=worktree_removed,
        branch_removed=branch_removed,
        errors=result.errors,
    )

    if result.ok:
        # Say which of the two things actually happened.
        logger.info(
            "removed worktree for %s (%s)",
            child_id,
            f"branch {branch} deleted" if delete_branch else f"branch {branch} kept",
        )
    else:
        logger.error(
            "failed to remove worktree for %s (branch %s): %s",
            child_id,
            branch,
            "; ".join(result.errors),
        )

    return result


# ---- named shared worktrees ---------------------------------------------


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

    check = subprocess.run(
        ["git", "rev-parse", "--verify", branch],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
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

    result = subprocess.run(
        args,
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
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
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=expected_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
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

    wt_result = subprocess.run(
        ["git", "worktree", "remove", "--force", wt_path],
        cwd=real_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    worktree_removed = wt_result.returncode == 0

    if not worktree_removed:
        stderr = wt_result.stderr.strip()
        logger.warning("git worktree remove failed for named worktree %s: %s", name, stderr)
        prune_result = subprocess.run(
            ["git", "worktree", "prune", "--verbose"],
            cwd=real_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
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
        br_result = subprocess.run(
            ["git", "branch", "-D", branch],
            cwd=real_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch_removed = br_result.returncode == 0

        if not branch_removed:
            stderr = br_result.stderr.strip()
            verify = subprocess.run(
                ["git", "rev-parse", "--verify", branch],
                cwd=real_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if verify.returncode != 0:
                branch_removed = True
            else:
                result.errors.append(f"branch delete: {stderr}")
                logger.warning(
                    "git branch -D failed for named worktree %s (branch %s): %s",
                    name,
                    branch,
                    stderr,
                )

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
