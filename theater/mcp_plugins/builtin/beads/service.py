"""Authenticated play-scoped task operations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from theater.plugin_client import TheaterPluginClient, TheaterPluginError

from . import links
from .backend import BeadsError, Board
from .context import _assignee, _assignment, _context, _members, _reassign
from .lifecycle import _reopen_args
from .locking import board_lock, drain_cancelled_worker
from .projection import _detail, _edges, _summary


def _text(value: str, field: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise BeadsError(f"{field} must be text of at most {maximum} characters")
    if required and not value.strip():
        raise BeadsError(f"{field} is required")
    return value


class _NeedsFacts:
    """A plan's request for one bounded job-facts read before it finishes."""

    __slots__ = ("context", "handles")

    def __init__(self, handles: list[str], context: dict[str, Any]) -> None:
        self.handles = handles
        self.context = context


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

    async def _operate_phased(
        self,
        permission: str,
        plan: Callable[[Board, str, tuple[set[str], dict[str, str]]], Any],
        finish: Callable[
            [Board, str, tuple[set[str], dict[str, str]], Any, dict[str, dict[str, Any]]],
            dict[str, Any],
        ],
    ) -> dict[str, Any]:
        """Locked board work in phases, with one bounded facts read between them.

        The plan runs first under the same freshness, lock and cancellation
        guarantees as _operate. It either returns a final result or asks for
        job facts, which are read outside the worker thread; the finish phase
        then re-enters the thread holding the same board lock, so the board it
        validated is still current.
        """
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
                outcome = await self._phase(board, actor, members, plan)
                if not isinstance(outcome, _NeedsFacts):
                    return outcome
                facts = await self._fetch_facts(client, outcome.handles)
                context = outcome.context

                def complete(board: Board, actor: str, members: Any) -> dict[str, Any]:
                    return finish(board, actor, members, context, facts)

                return await self._phase(board, actor, members, complete)

    async def _phase(
        self,
        board: Board,
        actor: str,
        members: tuple[set[str], dict[str, str]],
        action: Callable[[Board, str, tuple[set[str], dict[str, str]]], Any],
    ) -> Any:
        worker = asyncio.create_task(asyncio.to_thread(self._run, board, actor, members, action))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            await drain_cancelled_worker(worker)
            raise

    @staticmethod
    async def _fetch_facts(
        client: TheaterPluginClient, handles: list[str]
    ) -> dict[str, dict[str, Any]]:
        """One bounded facts read per batch of stored handles."""
        by_handle: dict[str, dict[str, Any]] = {}
        try:
            for start in range(0, len(handles), links.FACTS_BATCH):
                batch = handles[start : start + links.FACTS_BATCH]
                for row in await client.get_job_facts(batch):
                    if isinstance(row, dict) and isinstance(row.get("handle"), str):
                        by_handle[row["handle"]] = row
        except TheaterPluginError as exc:
            raise BeadsError(f"job facts are unavailable: {exc}") from exc
        return by_handle

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

    async def get_task(
        self, task_id: str, view: Literal["detail", "progress"] = "detail"
    ) -> dict[str, Any]:
        if view not in ("detail", "progress"):
            raise BeadsError("view must be detail or progress")
        if view == "detail":

            def action(board, _actor, _members):
                rows = board.rows()
                board.require_id(task_id, rows)
                return _detail(board, task_id, rows, board.ready_ids())

            return await self._operate("read", action)

        def plan(board, _actor, _members):
            rows = board.rows()
            ready = board.ready_ids()
            board.require_id(task_id, rows)
            row = rows[task_id]
            state = links.task_state(row)
            handles = links.facts_handles(state["links"])
            if not handles:
                return links.progress_view(board, task_id, row, rows, ready, {})
            return _NeedsFacts(handles, {"row": row, "rows": rows, "ready": ready})

        def finish(board, _actor, _members, context, facts):
            return links.progress_view(
                board, task_id, context["row"], context["rows"], context["ready"], facts
            )

        return await self._operate_phased("read", plan, finish)

    async def link_task_job(
        self,
        task_id: str,
        handle: str,
        role: Literal["work", "review"],
        review_of: str | None = None,
    ) -> dict[str, Any]:
        """Associate one dispatched job with this task; duplicates replay, never edit."""
        handle = links.handle_text(handle)
        if role not in links.ROLES:
            raise BeadsError("role must be work or review")
        if role == "work":
            if review_of is not None:
                raise BeadsError("a work link cannot name review_of")
        else:
            review_of = links.handle_text(review_of, "review_of")

        def plan(board, _actor, _members):
            rows = board.rows()
            board.require_id(task_id, rows)
            row = rows[task_id]
            prepared = links.plan_link(row, handle, role, review_of)
            if prepared["duplicate"]:
                record = prepared["record"]
                return self._link_result(
                    board, task_id, handle, record, True, prepared["links_total"]
                )
            state = prepared["state"]
            return _NeedsFacts(
                [handle],
                {
                    "row_status": row.get("status"),
                    "close": links.close_epoch(row),
                    "state": state,
                },
            )

        def finish(board, actor, _members, context, facts):
            created_at = links.authorize_new_link(facts.get(handle), board.instance_id, handle)
            links.guard_new_link(
                created_at, role, context["state"], context["row_status"], context["close"]
            )
            record = {
                "version": links.LINK_VERSION,
                "handle": handle,
                "role": role,
                "review_of": review_of,
                "generation": context["state"]["generation"],
                "created_at": created_at,
            }
            links.persist_link(board, task_id, context["state"]["links"], record, actor)
            return self._link_result(
                board, task_id, handle, record, False, len(context["state"]["links"]) + 1
            )

        return await self._operate_phased("write", plan, finish)

    @staticmethod
    def _link_result(
        board: Board,
        task_id: str,
        handle: str,
        record: dict[str, Any],
        duplicate: bool,
        links_total: int,
    ) -> dict[str, Any]:
        return {
            "play_instance_id": board.instance_id,
            "task_id": task_id,
            "handle": handle,
            "role": record["role"],
            "review_of": record["review_of"],
            "generation": record["generation"],
            "duplicate": duplicate,
            "links_total": links_total,
        }

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
            # local_id rejects non-strings too, but the isinstance also narrows the type
            if not isinstance(task_id, str) or not board.local_id(task_id):
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
                json.dumps({"theater_outcome": outcome, links.CLOSED_AT_KEY: time.time()}),
                "--append-notes",
                "Outcome: " + outcome,
                actor=actor,
            )
            rows = board.rows()
            return _detail(board, task_id, rows, board.ready_ids())

        return await self._operate("write", action)
