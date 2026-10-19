"""Bounded task views and local graph relationships."""

from __future__ import annotations

import json
from typing import Any

from .backend import Board

OUTCOME_KEY = "theater_outcome"
OUTCOME_STATUS_KEY = "theater_outcome_status"
OUTCOME_STATUSES = ("succeeded", "failed", "cancelled")
HISTORY_KEY = "theater_outcome_history"


def _history_items(metadata: dict[str, Any]) -> list[Any]:
    """The stored mixed outcome history; bd keeps complex metadata as text."""
    history = metadata.get(HISTORY_KEY) or []
    if isinstance(history, str):
        try:
            history = json.loads(history)
        except json.JSONDecodeError:
            history = []
    return history if isinstance(history, list) else []


def _history_item(item: Any) -> tuple[str, str | None]:
    """One stored entry as (text, status): strings mean unknown, objects carry a type."""
    if isinstance(item, dict):
        status = item.get("status")
        return str(item.get("outcome"))[:4000], status if status in OUTCOME_STATUSES else None
    return str(item)[:4000], None


def _outcome_status(row: dict[str, Any]) -> str | None:
    """The typed outcome, only while closed; unreadable stays unknown, never success."""
    if row.get("status") != "closed":
        return None
    metadata = row.get("metadata") or {}
    value = metadata.get(OUTCOME_STATUS_KEY) if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and value in OUTCOME_STATUSES else None


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


def _summary(
    board: Board, row: dict[str, Any], rows: dict[str, dict[str, Any]], ready: set[str]
) -> dict[str, Any]:
    parent = row.get("parent")
    return {
        "id": row["id"],
        "title": str(row.get("title", ""))[:500],
        "kind": row.get("issue_type"),
        "status": row.get("status"),
        "outcome_status": _outcome_status(row),
        "priority": row.get("priority"),
        "assignee": str(row["assignee"])[:128] if row.get("assignee") else None,
        "parent_id": parent
        if isinstance(parent, str) and board.local_id(parent) and parent in rows
        else None,
        "ready": row["id"] in ready,
    }


def _blockers(
    board: Board, rows: dict[str, dict[str, Any]], task_id: str
) -> tuple[list[str], list[str], list[str]]:
    """Local edges, children, and the ids of those still blocking this task."""
    deps = _edges(board, rows, task_id)
    children = sorted(key for key, value in rows.items() if value.get("parent") == task_id)
    blockers = [key for key in deps if rows[key].get("status") != "closed"]
    blockers.extend(key for key in children if rows[key].get("status") != "closed")
    return deps, children, sorted(set(blockers))


def _failed_prerequisites(
    rows: dict[str, dict[str, Any]], deps: list[str], children: list[str]
) -> list[dict[str, str]]:
    """Advisory: prerequisites that closed as failed or cancelled, sorted, never gated on.

    A closed prerequisite with unknown status is never taken for success; it just
    stays outside this list, which names known failures only. Native readiness
    and close guards are unaffected by it.
    """
    statuses: dict[str, str] = {}
    for task_id in [*deps, *children]:
        row = rows.get(task_id)
        status = _outcome_status(row) if row is not None else None
        if status in ("failed", "cancelled"):
            statuses[task_id] = status
    return [{"id": task_id, "outcome_status": statuses[task_id]} for task_id in sorted(statuses)]


def _detail(
    board: Board, task_id: str, rows: dict[str, dict[str, Any]], ready: set[str]
) -> dict[str, Any]:
    row = rows[task_id]
    deps, children, blockers = _blockers(board, rows, task_id)
    failed = _failed_prerequisites(rows, deps, children)
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    history = [_history_item(item) for item in _history_items(metadata)[:50]]
    return {
        "play_instance_id": board.instance_id,
        **_summary(board, row, rows, ready),
        "description": str(row.get("description") or "")[:16000],
        "deps": deps[:200],
        "blockers": sorted(set(blockers))[:200],
        "children": children[:200],
        "failed_prerequisites": failed[:200],
        "notes": str(row.get("notes") or "")[:16000],
        "outcome": str(metadata[OUTCOME_KEY])[:4000]
        if row.get("status") == "closed" and metadata.get(OUTCOME_KEY)
        else None,
        "outcome_history": [text for text, _status in history],
        "outcome_status_history": [status for _text, status in history],
        "truncated": (
            len(deps) > 200 or len(blockers) > 200 or len(children) > 200 or len(failed) > 200
        ),
    }
