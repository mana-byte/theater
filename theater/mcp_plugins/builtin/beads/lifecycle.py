"""Reopening guards and retained task outcomes."""

from __future__ import annotations

import json
from typing import Any

from . import links
from .backend import BeadsError, Board
from .projection import (
    HISTORY_KEY,
    OUTCOME_KEY,
    OUTCOME_STATUS_KEY,
    _edges,
    _history_items,
    _outcome_status,
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
    row = rows[task_id]
    metadata = row.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    args = links.reopen_generation_args(row)
    if not metadata.get(OUTCOME_KEY):
        # Reopen still clears a stray status, so no typed outcome outlives a cycle.
        return [*args, "--unset-metadata", OUTCOME_STATUS_KEY]
    # A typed close is retained as {outcome, status}; unknown stays a plain string.
    entry: Any = metadata[OUTCOME_KEY]
    status = _outcome_status(row)
    if status is not None:
        entry = {"outcome": metadata[OUTCOME_KEY], "status": status}
    history = [*_history_items(metadata), entry][-50:]
    return [
        *args,
        "--set-metadata",
        f"{HISTORY_KEY}=" + json.dumps(history),
        "--unset-metadata",
        OUTCOME_KEY,
        "--unset-metadata",
        OUTCOME_STATUS_KEY,
    ]
