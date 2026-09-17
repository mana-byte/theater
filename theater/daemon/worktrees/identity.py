"""Exact Git facts used before workspace creation and cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from theater.constants.worktree import GIT_QUERY_TIMEOUT_SECONDS
from theater.daemon.worktrees.repository import _git
from theater.models import BadRequest, WorkspaceRecord


@dataclass(frozen=True, slots=True)
class ExistingPathFacts:
    path: str
    repository_root: str | None
    canonical_repository_root: str | None
    branch: str | None
    head_commit: str | None


@dataclass(frozen=True, slots=True)
class WorktreeCreationFacts:
    initiating_repository_root: str
    canonical_repository_root: str
    resolved_base_commit: str


@dataclass(frozen=True, slots=True)
class WorktreeInspection:
    path: str
    canonical_repository_root: str
    branch: str
    head_commit: str
    dirty: bool


class GitFactsError(BadRequest):
    """Git could not establish a safe, exact workspace identity."""


def inspect_existing_path(path: str) -> ExistingPathFacts:
    resolved = _directory(path)
    top = _query(["git", "rev-parse", "--show-toplevel"], cwd=resolved, non_repo=True)
    if top is None:
        return ExistingPathFacts(resolved, None, None, None, None)
    common = _required_query(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=resolved
    )
    head = _required_query(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=resolved)
    branch = _branch(resolved)
    return ExistingPathFacts(
        path=resolved,
        repository_root=str(Path(top).resolve()),
        canonical_repository_root=str(Path(common).resolve().parent),
        branch=branch,
        head_commit=head,
    )


def resolve_creation_facts(path: str, base_ref: str | None = None) -> WorktreeCreationFacts:
    existing = inspect_existing_path(path)
    if existing.repository_root is None or existing.canonical_repository_root is None:
        raise GitFactsError(f"workspace path {existing.path!r} is not inside a Git repository")
    reference = "HEAD" if base_ref is None else base_ref
    commit = _required_query(
        ["git", "rev-parse", "--verify", f"{reference}^{{commit}}"], cwd=existing.path
    )
    return WorktreeCreationFacts(
        initiating_repository_root=existing.repository_root,
        canonical_repository_root=existing.canonical_repository_root,
        resolved_base_commit=commit,
    )


def inspect_registered_worktree(record: WorkspaceRecord) -> WorktreeInspection:
    if record.canonical_repository_root is None or record.branch is None:
        raise GitFactsError("Theater-owned workspaces require a stored repository root and branch")
    facts = inspect_existing_path(record.path)
    if facts.repository_root is None or facts.head_commit is None:
        raise GitFactsError(
            f"workspace {record.workspace_id!r} path disappeared or is no longer a Git worktree"
        )
    expected_path = str(Path(record.path).resolve())
    if facts.path != expected_path:
        raise GitFactsError("the stored workspace path no longer resolves to its recorded path")
    expected_root = str(Path(record.canonical_repository_root).resolve())
    if facts.canonical_repository_root != expected_root:
        raise GitFactsError("the workspace now belongs to a different canonical repository")
    if facts.repository_root != expected_path:
        raise GitFactsError("the stored workspace path is not its Git worktree root")
    if facts.branch != record.branch:
        raise GitFactsError("the workspace has a different branch checked out")
    listed_branch = _listed_worktree_branch(expected_root, expected_path)
    if listed_branch != record.branch:
        raise GitFactsError("Git worktree metadata does not match the stored path and branch")
    status = _query(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=expected_path,
        allow_empty=True,
    )
    return WorktreeInspection(
        path=expected_path,
        canonical_repository_root=expected_root,
        branch=record.branch,
        head_commit=facts.head_commit,
        dirty=bool(status),
    )


def validate_canonical_repository(path: str) -> str:
    resolved = _directory(path)
    common = _required_query(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=resolved
    )
    canonical = str(Path(common).resolve().parent)
    if canonical != resolved:
        raise GitFactsError(
            "stored canonical repository root no longer identifies the main checkout"
        )
    return canonical


def _directory(path: str) -> str:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise GitFactsError(f"workspace path {path!r} does not exist: {exc}") from exc
    if not resolved.is_dir():
        raise GitFactsError(f"workspace path {path!r} is not a directory")
    return str(resolved)


def _branch(path: str) -> str | None:
    result = _git(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=path,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return None
    raise GitFactsError(_git_error("read the checked-out branch", result.returncode, result.stderr))


def _query(
    argv: list[str], *, cwd: str, non_repo: bool = False, allow_empty: bool = False
) -> str | None:
    result = _git(
        argv,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        value = result.stdout.strip()
        if value or allow_empty:
            return value
        raise GitFactsError(f"Git returned no value for {' '.join(argv[1:])}")
    if non_repo and result.returncode == 128 and "not a git repository" in result.stderr.lower():
        return None
    raise GitFactsError(_git_error("inspect repository identity", result.returncode, result.stderr))


def _required_query(argv: list[str], *, cwd: str) -> str:
    value = _query(argv, cwd=cwd)
    assert value is not None
    return value


def _listed_worktree_branch(canonical_root: str, expected_path: str) -> str | None:
    output = _required_query(["git", "worktree", "list", "--porcelain", "-z"], cwd=canonical_root)
    current_path: str | None = None
    for field in output.split("\0"):
        if field.startswith("worktree "):
            current_path = str(Path(field.removeprefix("worktree ")).resolve())
        elif current_path == expected_path and field.startswith("branch refs/heads/"):
            return field.removeprefix("branch refs/heads/")
        elif not field:
            current_path = None
    return None


def _git_error(action: str, returncode: int, stderr: str) -> str:
    detail = stderr.strip() or "no diagnostic"
    return f"could not {action}; Git returned {returncode}: {detail}"


__all__ = [
    "ExistingPathFacts",
    "GitFactsError",
    "WorktreeCreationFacts",
    "WorktreeInspection",
    "inspect_existing_path",
    "inspect_registered_worktree",
    "resolve_creation_facts",
    "validate_canonical_repository",
]
