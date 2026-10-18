"""Authenticated membership and advisory task assignment."""

from __future__ import annotations

from typing import Any

from .backend import BeadsError, Board


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
