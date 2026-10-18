"""A tool body says hello before its first daemon call, or the daemon cannot name the caller."""

from __future__ import annotations

import pytest

from tests.support.mcp_tools import session
from theater.mcp import tools

CASES = {
    # the reply has nowhere to go without an identity
    "send": (
        {"send": {"handle": "p-you#1"}},
        lambda s: tools.send_prompt(s, target_id="p-you", prompt="hello"),
        ["hello", "send"],
    ),
    "update_session_settings": (
        {"participant.settings.update": {"applied": True}},
        lambda s: tools.update_session_settings(s, target="p-child", model="opus-5"),
        ["hello", "participant.settings.update"],
    ),
    "get_session_controls": (
        {"participant.controls": {}},
        lambda s: tools.get_session_controls(s, target="p-child"),
        ["hello", "participant.controls"],
    ),
    # the daemon authorizes by the named caller
    "terminate_session": (
        {"participant.kill": {"id": "p-child", "killed": True}},
        lambda s: tools.terminate_session(s, target="p-child"),
        ["hello", "participant.kill"],
    ),
    "adopt_session": (
        {"participant.adopt": {"id": "p-sib", "parent_id": "p-me"}},
        lambda s: tools.adopt_session(s, target="p-sib"),
        ["hello", "participant.adopt"],
    ),
    # caller_cwd is what the daemon uses for the privacy wall
    "recall": ({"recall": {}}, lambda s: tools.recall(s, paths=["x.py"]), ["hello", "recall"]),
    "recall_read": (
        {"recall_read": {}},
        lambda s: tools.recall_read(s, segment_id="codex-a41f"),
        ["hello", "recall_read"],
    ),
}


@pytest.mark.parametrize("tool", CASES)
async def test_a_tool_identifies_before_its_first_daemon_call(tool):
    replies, call, methods = CASES[tool]
    s = session(**replies)
    await call(s)
    assert s.client.methods == methods
