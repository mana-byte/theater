"""An MCP tool call obeys the approval policy, and a refusal from Theater's own server arrives."""

from __future__ import annotations

import pytest

from harness_compat.support.tool_effects import (
    expect_gated,
    expect_runs,
    mcp_effect,
    refusal_effect,
)

pytestmark = [pytest.mark.integration, pytest.mark.family("mcp")]

_APPROVAL = {"gated": "manual", "call": "yolo", "refusal": "yolo"}


async def test_an_mcp_tool_call_follows_the_approval_policy(live, obligation):
    participant = await live.spawn(_APPROVAL[obligation.detail])
    await live.ready(participant)

    if obligation.detail == "gated":
        await expect_gated(live, participant, await mcp_effect(live, participant))
    elif obligation.detail == "call":
        await expect_runs(live, participant, await mcp_effect(live, participant))
    else:
        await expect_runs(live, participant, await refusal_effect(live, participant))
