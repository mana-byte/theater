"""Tool bodies, kept free of any MCP framework so they can be tested directly.

Re-exports the tool functions and Session from their new homes in session.py
and toolsets/. Import paths here are preserved so existing callers — the server
module and the test suite — continue to work unchanged.
"""

from __future__ import annotations

from theater.mcp.session import Session
from theater.mcp.toolsets.delegation import (
    await_sessions,
    harnesses,
    models,
    put_child_back_in_the_wound,
    scratchpad_get,
    scratchpad_write,
    send_prompt,
    spawn_session,
)
from theater.mcp.toolsets.participants import _summarise, list_participants, register_pane, whoami
from theater.mcp.toolsets.recall import recall, recall_read
from theater.mcp.toolsets.transcripts import read_transcript

__all__ = [
    "Session",
    "_summarise",
    "await_sessions",
    "harnesses",
    "list_participants",
    "models",
    "put_child_back_in_the_wound",
    "read_transcript",
    "recall",
    "recall_read",
    "register_pane",
    "scratchpad_get",
    "scratchpad_write",
    "send_prompt",
    "spawn_session",
    "whoami",
]
