"""Stdio MCP tools for the authenticated Beads board."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Literal

from mcp.server import MCPServer

from .retention import BoardRetention
from .service import BeadsService


def build(
    state_dir: Path, *, board_ttl_seconds: int = 604800, bd_path: str | None = None
) -> MCPServer:
    service = BeadsService(state_dir, bd_path=bd_path)
    retention = BoardRetention(state_dir, board_ttl_seconds)

    @asynccontextmanager
    async def lifespan(_server):
        task = asyncio.create_task(retention.run())
        try:
            yield None
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    server = MCPServer("beads", lifespan=lifespan)

    @server.tool()
    async def list_tasks(
        view: Literal["all", "ready", "mine"] = "all",
        status: Literal["open", "in_progress", "blocked", "closed"] | None = None,
        kind: Literal["task", "epic", "milestone"] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict:
        """List this play's tasks, with bounded cursor pagination."""
        return await service.list_tasks(view, status, kind, cursor, limit)

    @server.tool()
    async def get_task(task_id: str, view: Literal["detail", "progress"] = "detail") -> dict:
        """Show a task and its local relationships, or its linked-job progress."""
        return await service.get_task(task_id, view)

    @server.tool()
    async def create_task(
        title: str,
        description: str = "",
        kind: Literal["task", "epic", "milestone"] = "task",
        priority: int = 2,
        parent_id: str | None = None,
        assignee: str | None = None,
        dependencies: list[str] | None = None,
    ) -> dict:
        """Create a task in this play; assignees must be live members."""
        return await service.create_task(
            title, description, kind, priority, parent_id, assignee, dependencies
        )

    @server.tool()
    async def update_task(
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        kind: Literal["task", "epic", "milestone"] | None = None,
        priority: int | None = None,
        status: Literal["open", "in_progress", "blocked"] | None = None,
        assignee: str | None = None,
        parent_id: str | None = None,
        note: str | None = None,
    ) -> dict:
        """Change task fields or append progress; use close_task to close."""
        return await service.update_task(
            task_id, title, description, kind, priority, status, assignee, parent_id, note
        )

    @server.tool()
    async def set_dependency(task_id: str, prerequisite_id: str, add: bool = True) -> dict:
        """Add or remove a local blocking dependency."""
        return await service.set_dependency(task_id, prerequisite_id, add)

    @server.tool()
    async def claim_task(task_id: str) -> dict:
        """Claim a ready task for the authenticated participant."""
        return await service.claim_task(task_id)

    @server.tool()
    async def close_task(task_id: str, outcome: str) -> dict:
        """Close through Beads' native guards and record the outcome."""
        return await service.close_task(task_id, outcome)

    @server.tool()
    async def link_task_job(
        task_id: str,
        handle: str,
        role: Literal["work", "review"],
        review_of: str | None = None,
    ) -> dict:
        """Link one of this play's dispatched jobs to a task as its work or review.

        A review link names the exact work job it reviews via review_of. An
        identical stored link is a no-op duplicate, safe to retry after a
        transport failure; changing a stored link is refused.
        """
        return await service.link_task_job(task_id, handle, role, review_of)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--board-ttl-seconds", type=int, default=604800)
    parser.add_argument("--bd-path")
    args = parser.parse_args(argv)
    if args.board_ttl_seconds < 0:
        parser.error("board TTL must be nonnegative")
    build(args.state_dir, board_ttl_seconds=args.board_ttl_seconds, bd_path=args.bd_path).run()


if __name__ == "__main__":
    main()
