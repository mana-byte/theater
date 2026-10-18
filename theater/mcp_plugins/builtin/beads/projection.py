"""Bounded task views and local graph relationships."""

from __future__ import annotations

import json
from typing import Any

from .backend import Board


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
