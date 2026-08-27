"""Claude native value conversion helpers."""

from __future__ import annotations

from theater.harness.normalization.values import revision_from
from theater.harness.normalization.values import (
    trajectory_identifier as _trajectory_id,
)


def _claude_mcp_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.startswith("mcp__"):
        return None
    server, separator, tool = value.removeprefix("mcp__").partition("__")
    if not separator:
        return None
    server_id = _trajectory_id(server)
    tool_id = _trajectory_id(tool)
    return (server_id, tool_id) if server_id is not None and tool_id is not None else None


def _claude_revision(record: dict) -> int:
    message = record.get("message")
    values = [record, message] if isinstance(message, dict) else [record]
    return revision_from(*values)


def _claude_block_native_id(
    block: dict, base_id: str | None, record_id: str | None, ordinal: int
) -> str | None:
    explicit = _trajectory_id(block.get("id"))
    if explicit is not None:
        return explicit
    if record_id is not None:
        return record_id if ordinal == 0 else f"{record_id}:block:{ordinal}"
    if base_id is not None:
        return base_id if ordinal == 0 else f"{base_id}:block:{ordinal}"
    return None


def _relativise(path: str, cwd: str | None) -> str | None:
    if not path:
        return None
    if not path.startswith("/"):
        return path
    if cwd is None:
        return None
    c = cwd.rstrip("/") + "/"
    if not (path == cwd or path.startswith(c)):
        return None
    return "." if path == cwd else path[len(c) :]
