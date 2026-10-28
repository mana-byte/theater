"""The harness's own edit tool is gated under manual and runs under the edits approval."""

from __future__ import annotations

import pytest

from harness_compat.support.tool_effects import edit_effect, expect_gated, expect_runs

pytestmark = [pytest.mark.integration, pytest.mark.family("edit")]

_APPROVAL = {"gated": "manual", "runs": "edits"}


async def test_a_native_edit_follows_the_approval_policy(live, obligation):
    participant = await live.spawn(_APPROVAL[obligation.detail])
    await live.ready(participant)
    effect = edit_effect(live)

    if obligation.detail == "gated":
        await expect_gated(live, participant, effect)
    else:
        await expect_runs(live, participant, effect)
