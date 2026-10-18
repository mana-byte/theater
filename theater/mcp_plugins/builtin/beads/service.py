"""Authenticated play-scoped task operations."""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import os
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal

from theater.plugin_client import TheaterPluginClient

from .backend import BeadsError, Board


def _text(value: str, field: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise BeadsError(f"{field} must be text of at most {maximum} characters")
    if required and not value.strip():
        raise BeadsError(f"{field} is required")
    return value


@asynccontextmanager
async def board_lock(state_dir: Path, instance_id: str):
    lock_dir = state_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    path = lock_dir / f"{instance_id}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


async def drain_cancelled_worker(worker: asyncio.Task[Any]) -> None:
    while not worker.done():
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(worker)
    with suppress(asyncio.CancelledError):
        worker.exception()


def _context(raw: dict[str, Any], permission: str) -> tuple[str, str]:
    play = raw.get("play")
    if not raw.get("enabled") or raw.get("plugin_name") != "beads":
        raise BeadsError("Beads is not enabled for this participant")
    if not isinstance(play, dict) or not isinstance(raw.get("participant_id"), str):
        raise BeadsError("caller is not an actual member of a play")
    if "beads" not in play.get("plugins", ()) or permission not in play.get("permissions", ()):
        raise BeadsError(f"this play position has no Beads {permission} grant")
    if not isinstance(play.get("position"), str) or not isinstance(play.get("instance_id"), str):
        raise BeadsError("play context is incomplete")
    return raw["participant_id"], play["instance_id"]


def _members(raw: list[dict[str, Any]]) -> tuple[set[str], dict[str, str]]:
    ids: set[str] = set()
    names: dict[str, str] = {}
    for member in raw:
        identifier = member.get("participant_id")
        if isinstance(identifier, str):
            ids.add(identifier)
            name = member.get("name")
            if isinstance(name, str) and name:
                names[name] = identifier
    return ids, names


def _assignee(value: str, members: tuple[set[str], dict[str, str]]) -> str:
    ids, names = members
    if value in ids:
        return value
    if value in names:
        return names[value]
    raise BeadsError("assignee must be a live member ID or name in this play")


def _assignment(
    row: dict[str, Any], value: str, members: tuple[set[str], dict[str, str]]
) -> tuple[str, bool]:
    assigned = _assignee(value, members) if value else ""
    force = row.get("status") == "in_progress" and row.get("assignee") not in (
        None,
        "",
        assigned,
    )
    return assigned, force


def _reassign(board: Board, task_id: str, actor: str, assigned: str) -> None:
    row = board.rows()[task_id]
    args = ["update", task_id, "--assignee", assigned]
    if row.get("status") == "in_progress" and row.get("assignee") not in (None, "", assigned):
        args.append("--force")
    board.command(*args, actor=actor)


def _edges(board: Board, rows: dict[str, dict[str, Any]], task_id: str) -> list[str]:
    deps = rows[task_id].get("dependencies") or []
    return sorted(
        {
            edge["depends_on_id"]
            for edge in deps
            if isinstance(edge, dict)
            and edge.get("type") == "blocks"
            and isinstance(edge.get("depends_on_id"), str)
            and board.local_id(edge["depends_on_id"])
            and edge["depends_on_id"] in rows
        }
    )


def _closed_reopen_obstacles(
    board: Board, rows: dict[str, dict[str, Any]], task_id: str
) -> list[str]:
    visited: set[str] = {task_id}
    parent = rows[task_id].get("parent")
    while isinstance(parent, str) and parent in rows and parent not in visited:
        visited.add(parent)
        parent = rows[parent].get("parent")
    pending = [task_id]
    dependent_seen = {task_id}
    while pending:
        current = pending.pop()
        for dependent in rows:
            if current in _edges(board, rows, dependent) and dependent not in dependent_seen:
                dependent_seen.add(dependent)
                visited.add(dependent)
                pending.append(dependent)
    return sorted(key for key in visited - {task_id} if rows[key].get("status") == "closed")


def _reopen_args(
    board: Board, rows: dict[str, dict[str, Any]], task_id: str, status: str | None
) -> list[str]:
    if status is None or rows[task_id].get("status") != "closed":
        return []
    closed = _closed_reopen_obstacles(board, rows, task_id)
    if closed:
        raise BeadsError("reopen closed parents and dependent tasks first: " + ", ".join(closed))
    metadata = rows[task_id].get("metadata") or {}
    if not isinstance(metadata, dict) or not metadata.get("theater_outcome"):
        return []
    history = metadata.get("theater_outcome_history") or []
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except json.JSONDecodeError:
            history = []
    if not isinstance(history, list):
        history = []
    history = [*history, metadata["theater_outcome"]][-50:]
    return [
        "--set-metadata",
        "theater_outcome_history=" + json.dumps(history),
        "--unset-metadata",
        "theater_outcome",
    ]


def _summary(
    board: Board, row: dict[str, Any], rows: dict[str, dict[str, Any]], ready: set[str]
) -> dict[str, Any]:
    parent = row.get("parent")
    return {
        "id": row["id"],
        "title": str(row.get("title", ""))[:500],
        "kind": row.get("issue_type"),
        "status": row.get("status"),
        "priority": row.get("priority"),
        "assignee": str(row["assignee"])[:128] if row.get("assignee") else None,
        "parent_id": parent
        if isinstance(parent, str) and board.local_id(parent) and parent in rows
        else None,
        "ready": row["id"] in ready,
    }


def _detail(
    board: Board, task_id: str, rows: dict[str, dict[str, Any]], ready: set[str]
) -> dict[str, Any]:
    row = rows[task_id]
    deps = _edges(board, rows, task_id)
    children = sorted(key for key, value in rows.items() if value.get("parent") == task_id)
    blockers = [key for key in deps if rows[key].get("status") != "closed"]
    blockers.extend(key for key in children if rows[key].get("status") != "closed")
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    history = metadata.get("theater_outcome_history") or []
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except json.JSONDecodeError:
            history = []
    if not isinstance(history, list):
        history = []
    return {
        "play_instance_id": board.instance_id,
        **_summary(board, row, rows, ready),
        "description": str(row.get("description") or "")[:16000],
        "deps": deps[:200],
        "blockers": sorted(set(blockers))[:200],
        "children": children[:200],
        "notes": str(row.get("notes") or "")[:16000],
        "outcome": str(metadata["theater_outcome"])[:4000]
        if row.get("status") == "closed" and metadata.get("theater_outcome")
        else None,
        "outcome_history": [str(item)[:4000] for item in history[:50]],
        "truncated": len(deps) > 200 or len(blockers) > 200 or len(children) > 200,
    }


class BeadsService:
    def __init__(self, state_dir: Path, *, bd_path: str | None = None) -> None:
        self.state_dir = state_dir.resolve()
        self.bd_path = bd_path

    async def _operate(
        self,
        permission: str,
        action: Callable[[Board, str, tuple[set[str], dict[str, str]]], dict[str, Any]],
    ) -> dict[str, Any]:
        async with TheaterPluginClient() as client:
            actor, instance_id = _context(await client.play_context(), permission)
            board = Board(self.state_dir, instance_id, self.bd_path)
            async with board_lock(self.state_dir, instance_id):
                fresh_actor, fresh_id = _context(await client.play_context(), permission)
                if (fresh_actor, fresh_id) != (actor, instance_id):
                    raise BeadsError("play membership changed while waiting for the board")
                members = (
                    _members(await client.play_members()) if permission == "write" else (set(), {})
                )
                worker = asyncio.create_task(
                    asyncio.to_thread(self._run, board, actor, members, action)
                )
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    await drain_cancelled_worker(worker)
                    raise

    @staticmethod
    def _run(
        board: Board,
        actor: str,
        members: tuple[set[str], dict[str, str]],
        action: Callable[[Board, str, tuple[set[str], dict[str, str]]], dict[str, Any]],
    ) -> dict[str, Any]:
        board.ensure()
        return action(board, actor, members)

    async def list_tasks(
        self,
        view: Literal["all", "ready", "mine"] = "all",
        status: Literal["open", "in_progress", "blocked", "closed"] | None = None,
        kind: Literal["task", "epic", "milestone"] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100:
            raise BeadsError("limit must be between 1 and 100")

        def action(board, actor, _members):
            rows = board.rows()
            ready = board.ready_ids()
            tasks = [
                _summary(board, row, rows, ready)
                for _, row in sorted(rows.items())
                if (status is None or row.get("status") == status)
                and (kind is None or row.get("issue_type") == kind)
                and (view != "ready" or row["id"] in ready)
                and (view != "mine" or row.get("assignee") == actor)
            ]
            digest = hashlib.sha256(json.dumps(tasks, sort_keys=True).encode()).hexdigest()[:16]
            offset = 0
            if cursor is not None:
                try:
                    if len(cursor) > 128:
                        raise BeadsError("invalid or stale task cursor")
                    payload = json.loads(base64.urlsafe_b64decode(cursor + "=="))
                    offset = payload["offset"]
                except (ValueError, KeyError, TypeError) as exc:
                    raise BeadsError("invalid or stale task cursor") from exc
                if payload["digest"] != digest or type(offset) is not int or offset < 0:
                    raise BeadsError("invalid or stale task cursor")
            page = tasks[offset : offset + limit]
            next_offset = offset + len(page)
            next_cursor = None
            if next_offset < len(tasks):
                raw = json.dumps({"offset": next_offset, "digest": digest}).encode()
                next_cursor = base64.urlsafe_b64encode(raw).decode().rstrip("=")
            return {
                "play_instance_id": board.instance_id,
                "tasks": page,
                "next_cursor": next_cursor,
            }

        return await self._operate("read", action)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        def action(board, _actor, _members):
            rows = board.rows()
            board.require_id(task_id, rows)
            return _detail(board, task_id, rows, board.ready_ids())

        return await self._operate("read", action)

    async def create_task(
        self,
        title: str,
        description: str = "",
        kind: Literal["task", "epic", "milestone"] = "task",
        priority: int = 2,
        parent_id: str | None = None,
        assignee: str | None = None,
        dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        title = _text(title, "title", 500, required=True)
        description = _text(description, "description", 16000)
        if type(priority) is not int or priority not in range(5):
            raise BeadsError("priority must be from 0 through 4")
        if kind not in ("task", "epic", "milestone"):
            raise BeadsError("kind must be task, epic, or milestone")
        dependencies = dependencies or []
        if len(dependencies) > 100 or len(set(dependencies)) != len(dependencies):
            raise BeadsError("dependencies must contain at most 100 distinct IDs")

        def action(board, actor, members):
            rows = board.rows()
            args = [
                "create",
                f"--title={title}",
                "--description",
                description,
                "--type",
                kind,
                "--priority",
                str(priority),
            ]
            if parent_id is not None:
                board.require_id(parent_id, rows)
                args.extend(("--parent", parent_id))
            if assignee is not None:
                args.extend(("--assignee", _assignee(assignee, members)))
            for dependency in dependencies:
                board.require_id(dependency, rows)
            if dependencies:
                args.extend(("--deps", ",".join(dependencies)))
            created = board.command(*args, actor=actor)
            task_id = created.get("id") if isinstance(created, dict) else None
            if not board.local_id(task_id):
                raise BeadsError("Beads created an invalid local task ID")
            rows = board.rows()
            board.require_id(task_id, rows)
            return _detail(board, task_id, rows, board.ready_ids())

        return await self._operate("write", action)

    async def update_task(
        self,
        task_id: str,
        title: str | None = None,
        description: str | None = None,
        kind: Literal["task", "epic", "milestone"] | None = None,
        priority: int | None = None,
        status: Literal["open", "in_progress", "blocked"] | None = None,
        assignee: str | None = None,
        parent_id: str | None = None,
        note: str | None = None,
    ) -> dict[str, Any]:
        if title is not None:
            title = _text(title, "title", 500, required=True)
        if description is not None:
            description = _text(description, "description", 16000)
        if note is not None:
            note = _text(note, "note", 4000, required=True)
        if priority is not None and (type(priority) is not int or priority not in range(5)):
            raise BeadsError("priority must be from 0 through 4")
        if kind is not None and kind not in ("task", "epic", "milestone"):
            raise BeadsError("kind must be task, epic, or milestone")
        if status is not None and status not in ("open", "in_progress", "blocked"):
            raise BeadsError("update_task cannot close a task; use close_task with an outcome")

        def action(board, actor, members):
            rows = board.rows()
            board.require_id(task_id, rows)
            args = ["update", task_id]
            for flag, value in (
                ("--title", title),
                ("--description", description),
                ("--type", kind),
                ("--priority", priority),
                ("--status", status),
                ("--append-notes", note),
            ):
                if value is not None:
                    args.extend((flag, str(value)))
            forced_assignment = None
            if assignee is not None:
                assigned, force = _assignment(rows[task_id], assignee, members)
                if force:
                    forced_assignment = assigned
                else:
                    args.extend(("--assignee", assigned))
            if parent_id is not None:
                if parent_id:
                    board.require_id(parent_id, rows)
                args.extend(("--parent", parent_id))
            args.extend(_reopen_args(board, rows, task_id, status))
            if len(args) == 2 and forced_assignment is None:
                raise BeadsError("update_task requires at least one change")
            if len(args) > 2:
                board.command(*args, actor=actor)
            if forced_assignment is not None:
                _reassign(board, task_id, actor, forced_assignment)
            rows = board.rows()
            return _detail(board, task_id, rows, board.ready_ids())

        return await self._operate("write", action)

    async def set_dependency(
        self, task_id: str, prerequisite_id: str, add: bool = True
    ) -> dict[str, Any]:
        def action(board, actor, _members):
            rows = board.rows()
            board.require_id(task_id, rows)
            board.require_id(prerequisite_id, rows)
            if task_id == prerequisite_id:
                raise BeadsError("a task cannot depend on itself")
            present = prerequisite_id in _edges(board, rows, task_id)
            if present != add:
                board.command(
                    "dep", "add" if add else "remove", task_id, prerequisite_id, actor=actor
                )
            return {
                "play_instance_id": board.instance_id,
                "task_id": task_id,
                "prerequisite_id": prerequisite_id,
                "present": add,
            }

        return await self._operate("write", action)

    async def claim_task(self, task_id: str) -> dict[str, Any]:
        def action(board, actor, _members):
            rows = board.rows()
            board.require_id(task_id, rows)
            if task_id not in board.ready_ids():
                raise BeadsError("task is not ready to claim")
            if rows[task_id].get("assignee") not in (None, "", actor):
                raise BeadsError("task is assigned to another member; reassign it explicitly")
            board.command(
                "update", task_id, "--assignee", actor, "--status", "in_progress", actor=actor
            )
            rows = board.rows()
            return _detail(board, task_id, rows, board.ready_ids())

        return await self._operate("write", action)

    async def close_task(self, task_id: str, outcome: str) -> dict[str, Any]:
        outcome = _text(outcome, "outcome", 4000, required=True)

        def action(board, actor, _members):
            rows = board.rows()
            board.require_id(task_id, rows)
            if rows[task_id].get("status") == "closed":
                raise BeadsError("task is already closed")
            board.command(
                "update",
                task_id,
                "--status",
                "closed",
                "--metadata",
                json.dumps({"theater_outcome": outcome}),
                "--append-notes",
                "Outcome: " + outcome,
                actor=actor,
            )
            rows = board.rows()
            return _detail(board, task_id, rows, board.ready_ids())

        return await self._operate("write", action)
