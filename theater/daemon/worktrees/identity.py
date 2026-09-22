"""Exact Git facts used before workspace creation and cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
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


@dataclass(frozen=True, slots=True)
class WorktreeMetadata:
    path: str
    branch: str | None


class CreationIntentState(StrEnum):
    """The only safe outcomes when inspecting a persisted creation intent."""

    READY = "ready"
    ABSENT = "absent"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class CreationIntentInspection:
    state: CreationIntentState
    worktree: WorktreeInspection | None = None


@dataclass(frozen=True, slots=True)
class PartialCreationInspection:
    canonical_repository_root: str
    path: str
    path_exists: bool
    branch_head: str | None
    metadata: WorktreeMetadata | None
    branch_metadata_paths: tuple[str, ...]


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
    metadata = _listed_worktree_metadata(expected_root, expected_path)
    if metadata is None or metadata.branch != record.branch:
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


def inspect_creation_intent(record: WorkspaceRecord) -> CreationIntentInspection:
    """Classify a creation intent without ever retrying its Git mutation.

    Absence is conclusive only when the deterministic path, branch, and Git
    worktree metadata are all absent. Everything else stays recoverable.
    """
    try:
        inspection = inspect_registered_worktree(record)
    except GitFactsError:
        return _missing_creation_intent(record)
    if inspection.head_commit == record.resolved_base_commit:
        return CreationIntentInspection(CreationIntentState.READY, inspection)
    return CreationIntentInspection(CreationIntentState.AMBIGUOUS)


def _missing_creation_intent(record: WorkspaceRecord) -> CreationIntentInspection:
    if record.canonical_repository_root is None or record.branch is None:
        return CreationIntentInspection(CreationIntentState.AMBIGUOUS)
    if Path(record.path).exists():
        return CreationIntentInspection(CreationIntentState.AMBIGUOUS)
    try:
        root = validate_canonical_repository(record.canonical_repository_root)
        branch_exists = _branch_exists(root, record.branch)
        metadata = _listed_worktree_metadata(root, str(Path(record.path).resolve()))
    except GitFactsError:
        return CreationIntentInspection(CreationIntentState.AMBIGUOUS)
    if branch_exists or metadata is not None:
        return CreationIntentInspection(CreationIntentState.AMBIGUOUS)
    return CreationIntentInspection(CreationIntentState.ABSENT)


def inspect_partial_creation(record: WorkspaceRecord) -> PartialCreationInspection:
    """Read exact facts required before resolving a pathless creation partial."""
    if record.canonical_repository_root is None or record.branch is None:
        raise GitFactsError("partial creation lacks durable repository or branch facts")
    root = validate_canonical_repository(record.canonical_repository_root)
    path = str(Path(record.path).resolve())
    entries = _listed_worktree_entries(root)
    metadata = next((entry for entry in entries if entry.path == path), None)
    return PartialCreationInspection(
        canonical_repository_root=root,
        path=path,
        path_exists=Path(record.path).exists(),
        branch_head=_branch_head(root, record.branch),
        metadata=metadata,
        branch_metadata_paths=tuple(
            entry.path for entry in entries if entry.branch == record.branch
        ),
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


def _listed_worktree_metadata(canonical_root: str, expected_path: str) -> WorktreeMetadata | None:
    return next(
        (
            entry
            for entry in _listed_worktree_entries(canonical_root)
            if entry.path == expected_path
        ),
        None,
    )


def _listed_worktree_entries(canonical_root: str) -> tuple[WorktreeMetadata, ...]:
    output = _required_query(["git", "worktree", "list", "--porcelain", "-z"], cwd=canonical_root)
    current_path: str | None = None
    current_branch: str | None = None
    entries: list[WorktreeMetadata] = []
    for field in output.split("\0"):
        if field.startswith("worktree "):
            if current_path is not None:
                entries.append(WorktreeMetadata(current_path, current_branch))
            current_path = str(Path(field.removeprefix("worktree ")).resolve())
            current_branch = None
        elif current_path is not None and field.startswith("branch refs/heads/"):
            current_branch = field.removeprefix("branch refs/heads/")
        elif not field:
            if current_path is not None:
                entries.append(WorktreeMetadata(current_path, current_branch))
            current_path = None
            current_branch = None
    if current_path is not None:
        entries.append(WorktreeMetadata(current_path, current_branch))
    return tuple(entries)


def _branch_exists(canonical_root: str, branch: str) -> bool:
    result = _git(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=canonical_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise GitFactsError(_git_error("inspect creation branch", result.returncode, result.stderr))


def _branch_head(canonical_root: str, branch: str) -> str | None:
    result = _git(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}"],
        cwd=canonical_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=GIT_QUERY_TIMEOUT_SECONDS,
    )
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return None
    raise GitFactsError(_git_error("inspect creation branch", result.returncode, result.stderr))


def _git_error(action: str, returncode: int, stderr: str) -> str:
    detail = stderr.strip() or "no diagnostic"
    return f"could not {action}; Git returned {returncode}: {detail}"


__all__ = [
    "CreationIntentInspection",
    "CreationIntentState",
    "ExistingPathFacts",
    "GitFactsError",
    "PartialCreationInspection",
    "WorktreeCreationFacts",
    "WorktreeInspection",
    "WorktreeMetadata",
    "inspect_creation_intent",
    "inspect_existing_path",
    "inspect_partial_creation",
    "inspect_registered_worktree",
    "resolve_creation_facts",
    "validate_canonical_repository",
]
