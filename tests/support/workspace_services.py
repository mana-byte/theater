"""Git repositories and durable workspace services for the workspace lifecycle tests."""

from __future__ import annotations

import itertools
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from theater.daemon.operations import OperationService
from theater.daemon.persistence.store import Store
from theater.daemon.worktrees.service import WorkspaceRequest, WorkspaceService


def git(repo: str | Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def make_repository(tmp_path: Path) -> str:
    """A git repository on ``main`` with one commit; returns its root path."""
    root = tmp_path / "repository"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("initial\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "initial")
    return str(root)


@contextmanager
def open_workspace_services(
    tmp_path: Path,
) -> Iterator[tuple[Store, OperationService, WorkspaceService]]:
    store = Store(tmp_path / "workspaces.db")
    operation_ids = (f"operation-{index}" for index in itertools.count(1))
    value_ids = (f"workspace-value-{index}" for index in itertools.count(1))
    operations = OperationService(store, id_factory=lambda: next(operation_ids))
    service = WorkspaceService(store, operations, id_factory=lambda: next(value_ids))
    try:
        yield store, operations, service
    finally:
        store.close()


def external_params(path: Path) -> dict[str, object]:
    return {
        "ownership_kind": "frontend",
        "owner_id": "frontend-a",
        "path": str(path),
    }


async def settled(operations: OperationService, accepted: object):
    assert isinstance(accepted, dict)
    record, timed_out = await operations.wait(str(accepted["operation_id"]), wait_seconds=5)
    assert not timed_out
    return record


async def persist_creation_intent(
    store: Store,
    service: WorkspaceService,
    request: WorkspaceRequest,
    *,
    reservation_id: str,
):
    preparation = await service.prepare_for_spawn(request)
    with store.write_unit() as unit:
        return service.reserve_for_spawn(
            preparation,
            reservation_id=reservation_id,
            owner_id="local_operator",
            connection=unit.connection,
        )
