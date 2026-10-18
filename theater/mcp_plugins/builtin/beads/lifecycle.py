"""Reopening guards and retained task outcomes."""

from __future__ import annotations

import json
from typing import Any

from .backend import BeadsError, Board
from .projection import _edges


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
