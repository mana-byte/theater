"""A native runtime is a physical route, falls back to the pane path, and refuses what it must."""

from __future__ import annotations

import uuid

import pytest

from scripts.harness_compat.contract import MockReply
from tests.support.live_rig import inconclusive, violation
from theater.frontend.client import FrontendResponseError

pytestmark = [pytest.mark.integration, pytest.mark.family("runtime")]


async def test_a_native_runtime_is_proven_by_its_physical_route(live, obligation):
    participant = await live.spawn(live.approval)
    await live.ready(participant)

    if obligation.detail == "native":
        await _native_turn(live, participant)
    elif obligation.detail == "legacy":
        await _legacy_turn(live, participant)
    else:
        await _refused(live, participant, obligation.detail.removeprefix("unavailable-"))


async def _turn(live, participant: str):
    nonce = f"ROUTE-{uuid.uuid4().hex[:8]}"
    live.queue(MockReply(nonce))

    job = await live.send(participant, f"Reply with {nonce}")

    assert job.state == "done", job.error
    assert nonce in str(job.result)
    seen = await live.participant(participant)
    assert seen.transcript_identity is not None and seen.transcript_identity.state == "trusted"
    return seen


async def _native_turn(live, participant: str) -> None:
    seen = await _turn(live, participant)
    if seen.native_route is None:
        violation("the harness declares a native runtime but the participant has no native route")
    assert seen.native_route.health == "connected", seen.native_route
    assert seen.native_route.native_session_id, "the native route carries no session identity"


async def _legacy_turn(live, participant: str) -> None:
    """Under a plugin whose probe refuses the native runtime, the pane path still carries a turn."""
    seen = await _turn(live, participant)
    if seen.native_route is not None:
        violation("a native route was bound although the runtime probe refused the release")
    assert seen.terminal_route is not None and seen.terminal_route.health == "healthy"


async def _refused(live, participant: str, capability: str) -> None:
    offered = (await live.controls(participant)).actions.get(capability)
    assert offered is None or not offered.admissible, f"{capability} is declared unavailable"
    if capability != "settings_update":
        inconclusive(f"no public call exercises the {capability!r} capability yet")
    models = live.provisioned.models
    key = uuid.uuid4().hex
    try:
        accepted = (
            await live.client.controls.update_settings(
                participant, idempotency_key=key, model=models[-1]
            )
        ).value
    except FrontendResponseError as refusal:
        assert "setting" in str(refusal).lower(), refusal
        return
    operation = await live._settled(accepted.operation_id)
    assert operation.state != "succeeded", (
        f"{capability} was accepted although declared unavailable"
    )
