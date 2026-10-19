"""Authenticated membership and advisory task assignment."""

from __future__ import annotations

from typing import Any

from .backend import BeadsError, Board

# Live members as (exact IDs, casefolded-name map, roster); the map holds a member
# ID for one live name or None when several members share the casefolded spelling.
MemberSnapshot = tuple[set[str], dict[str, str | None], list[tuple[str, str]]]

MAX_SUGGESTIONS = 20


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


def _members(raw: list[dict[str, Any]]) -> MemberSnapshot:
    """One membership snapshot; names casefold exactly as Theater resolves them."""
    ids: set[str] = set()
    aliases: dict[str, str | None] = {}
    roster: list[tuple[str, str]] = []
    for member in raw:
        identifier = member.get("participant_id")
        if not isinstance(identifier, str):
            continue
        ids.add(identifier)
        name = member.get("name")
        if isinstance(name, str) and name:
            roster.append((name, identifier))
            folded = name.casefold()
            if folded in aliases and aliases[folded] != identifier:
                aliases[folded] = None  # a shared spelling can never pick one member
            else:
                aliases[folded] = identifier
    return ids, aliases, roster


def _known_members(roster: list[tuple[str, str]]) -> str:
    """Deterministic bounded guidance so a refusal can name the real choices."""
    shown = sorted(roster)[:MAX_SUGGESTIONS]
    hint = ", ".join(f"{name} ({identifier})" for name, identifier in shown)
    if not hint:
        return ""
    suffix = f"; known members: {hint}"
    if len(roster) > MAX_SUGGESTIONS:
        suffix += f" and {len(roster) - MAX_SUGGESTIONS} more"
    return suffix


def _assignee(value: str, members: MemberSnapshot) -> str:
    ids, aliases, roster = members
    if value in ids:
        return value
    folded = value.casefold()
    if folded in aliases:
        member = aliases[folded]
        if member is None:
            raise BeadsError(
                f"assignee name {value!r} is shared by several live members "
                f"case-insensitively; use an exact member ID{_known_members(roster)}"
            )
        return member
    raise BeadsError(
        f"assignee must be a live member ID or name in this play{_known_members(roster)}"
    )


def _assignment(row: dict[str, Any], value: str, members: MemberSnapshot) -> tuple[str, bool]:
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
