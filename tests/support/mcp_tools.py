"""A fake daemon client behind an MCP session, and the structured half of a tool result."""

from __future__ import annotations

import json

from theater.mcp import tools

RECORD = {
    "id": "p-me",
    "name": "Arlequin",
    "description": "Coordinate the implementation",
    "harness": "vibe",
    "tier": "spawned",
    "status": "idle",
    "cwd": "/tmp/project",
    "branch": None,
    "session_id": "ses-me",
    "parent_id": None,
    "addressable": True,
    "tmux_pane": "%3",
    "pid": 4321,
    "resume_state": "live",
}


class FakeClient:
    """A daemon that answers from a dict and remembers what it was asked."""

    def __init__(self, **replies):
        self.replies = replies
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, **params):
        self.calls.append((method, params))
        return self.replies.get(method, RECORD)

    def params(self, method: str) -> dict:
        return next(p for m, p in self.calls if m == method)

    @property
    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def session(**replies) -> tools.Session:
    return tools.Session(participant_id="p-me", harness="vibe", client=FakeClient(**replies))


def resolved(**replies) -> tools.Session:
    s = session(**replies)
    s._resolved = True
    return s


def tool_payload(result):
    """Take the structured half of a CallToolResult.

    Theater structures small bare `-> dict` controls. The SDK wraps `-> list[dict]`
    output as {"result": [...]}; bulk bare-dict tools still return JSON text.
    """
    structured = result.structured_content
    if isinstance(structured, dict) and set(structured) == {"result"}:
        return structured["result"]
    if structured is not None:
        return structured
    return json.loads(result.content[0].text)
