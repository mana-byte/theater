"""An MCP tool call obeys the approval policy: gated under manual, run under yolo."""

from __future__ import annotations

import pytest

from tests.support.tool_effects import expect_gated, expect_runs, mcp_effect

pytestmark = [pytest.mark.integration, pytest.mark.family("mcp")]

_APPROVAL = {"gated": "manual", "call": "yolo"}


async def test_an_mcp_tool_call_follows_the_approval_policy(live, obligation):
    participant = await live.spawn(_APPROVAL[obligation.detail])
    await live.ready(participant)
    effect = await mcp_effect(live, participant)

    if obligation.detail == "gated":
        await expect_gated(live, participant, effect)
    else:
        await expect_runs(live, participant, effect)
