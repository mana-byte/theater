"""Every declared approval launches through the real planner and reaches a ready prompt."""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.family("launch")]


async def test_the_stock_cli_launches_and_reaches_a_ready_prompt(live, obligation):
    participant = await live.spawn(obligation.detail)

    await live.ready(participant)

    await live.expect_prompt(participant)
    assert (await live.participant(participant)).status != "dead"
