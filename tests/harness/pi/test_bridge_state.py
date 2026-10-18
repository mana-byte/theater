"""Pi frontend bridge status and identity: settle, pending questions and epoch ordering."""

from __future__ import annotations

import asyncio

from tests.support.pi_bridge import (
    SESSION_A,
    SESSION_B,
    ScriptedPiPeer,
    bridge_snapshot,
)
from tests.support.pi_bridge_results import eventually, make_runtime
from theater.harness.contracts.runtime import (
    RuntimeExecutionState,
)
from theater.models import Status


async def test_pi_frontend_agent_end_stays_working_until_agent_settled() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()
    source = runtime.live_source()
    await source.read()

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_start",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "active",
                "sequence": 1,
            },
        }
    )
    await eventually(lambda: runtime._execution_state is RuntimeExecutionState.ACTIVE)
    assert (await source.read()).status is Status.WORKING

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_end",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "active",
                "sequence": 2,
            },
        }
    )
    await asyncio.sleep(0)
    assert (await source.read()).status is Status.WORKING

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_settled",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "idle",
                "sequence": 3,
            },
        }
    )
    await eventually(lambda: runtime._execution_state is RuntimeExecutionState.IDLE)
    assert (await source.read()).status is Status.IDLE
    await runtime.aclose()


async def test_pi_frontend_pending_question_parks_status_on_awaiting_input() -> None:
    """A bridge-reported question is a display hint, never a control input.

    The extension reports a question tool blocked on the human as a pending
    interaction in its snapshot; the live status mirrors Codex's pending
    clarification and parks on AWAITING_INPUT until the answer clears it.
    """
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot(execution_state="active")})
    runtime = make_runtime(peer)
    await runtime.attach()
    source = runtime.live_source()
    assert (await source.read()).status is Status.WORKING
    assert runtime._runtime_snapshot().pending_interaction is None

    # The bridge pushes a fresh snapshot the moment the question opens: no
    # lifecycle event fires while the turn is parked on the human.
    peer.push(
        {
            "type": "snapshot",
            "snapshot": bridge_snapshot(
                execution_state="active",
                snapshot_revision=2,
                pending_interaction={
                    "kind": "clarification",
                    "native_turn_id": None,
                    "details": "ask_user_question",
                },
            ),
        }
    )
    await eventually(lambda: runtime._runtime_snapshot().pending_interaction is not None)
    parked = runtime._runtime_snapshot()
    assert parked.pending_interaction is not None
    assert parked.pending_interaction.kind.value == "clarification"
    assert parked.pending_interaction.details == "ask_user_question"
    assert (await source.read()).status is Status.AWAITING_INPUT

    # The matching tool result pushes the cleared state; the turn resumes.
    peer.push(
        {
            "type": "snapshot",
            "snapshot": bridge_snapshot(execution_state="active", snapshot_revision=3),
        }
    )
    await eventually(lambda: runtime._runtime_snapshot().pending_interaction is None)
    assert (await source.read()).status is Status.WORKING
    await runtime.aclose()


async def test_pi_frontend_ignores_old_session_settled_after_reload() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "session_shutdown",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "unknown",
                "sequence": 1,
            },
        }
    )
    await eventually(lambda: runtime._native_session_id is None)
    peer.push(
        {"type": "snapshot", "snapshot": bridge_snapshot(session_id=SESSION_B, bridge_epoch=3)}
    )
    await eventually(lambda: runtime._native_session_id == SESSION_B)
    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_settled",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "idle",
                "sequence": 2,
            },
        }
    )
    await asyncio.sleep(0)

    assert runtime._native_session_id == SESSION_B
    assert runtime._execution_state is RuntimeExecutionState.IDLE
    await runtime.aclose()


async def test_pi_frontend_delayed_old_history_cannot_roll_back_identity_or_idle() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()

    peer.push(
        {
            "type": "snapshot",
            "snapshot": bridge_snapshot(
                session_id=SESSION_B,
                bridge_epoch=3,
                model="anthropic/claude-test",
                thinking="low",
                execution_state="active",
            ),
        }
    )
    await eventually(lambda: runtime._native_session_id == SESSION_B)

    # A reconnect can deliver the prior socket's buffered history after the
    # successor snapshot.  Neither its session_start nor settled boundary may
    # re-establish A, re-enable old settings, or manufacture idle for B.
    peer.push(
        {
            "type": "history",
            "snapshot": bridge_snapshot(session_id=SESSION_A, bridge_epoch=2),
            "events": [
                {
                    "name": "session_start",
                    "native_session_id": SESSION_A,
                    "bridge_epoch": 2,
                    "execution_state": "idle",
                    "sequence": 0,
                },
                {
                    "name": "agent_settled",
                    "native_session_id": SESSION_A,
                    "bridge_epoch": 2,
                    "execution_state": "idle",
                    "sequence": 9,
                },
            ],
        }
    )
    await asyncio.sleep(0)

    assert runtime._native_session_id == SESSION_B
    assert runtime._bridge_epoch == 3
    assert runtime._execution_state is RuntimeExecutionState.ACTIVE
    assert runtime._settings.model == "anthropic/claude-test"
    assert runtime.live_source().health_snapshot()[0].dropped >= 1
    await runtime.aclose()


async def test_pi_frontend_rejects_same_epoch_conflicting_snapshot_and_event_identity() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()

    # Events do not establish identity, including a seemingly newer
    # session_start.  Only a snapshot with a strictly newer bridge epoch may.
    peer.push(
        {
            "type": "event",
            "event": {
                "name": "session_start",
                "native_session_id": SESSION_B,
                "bridge_epoch": 3,
                "execution_state": "idle",
                "sequence": 0,
            },
        }
    )
    peer.push(
        {
            "type": "snapshot",
            "snapshot": bridge_snapshot(session_id=SESSION_B, bridge_epoch=2),
        }
    )
    await asyncio.sleep(0)

    assert runtime._native_session_id == SESSION_A
    assert runtime._bridge_epoch == 2
    assert runtime._execution_state is RuntimeExecutionState.IDLE
    await runtime.aclose()


async def test_pi_frontend_delayed_same_session_history_cannot_regress_idle() -> None:
    peer = ScriptedPiPeer(responses={"pi.snapshot": bridge_snapshot()})
    runtime = make_runtime(peer)
    await runtime.attach()

    peer.push(
        {
            "type": "event",
            "event": {
                "name": "agent_start",
                "native_session_id": SESSION_A,
                "bridge_epoch": 2,
                "execution_state": "active",
                "sequence": 1,
            },
        }
    )
    peer.push(
        {
            "type": "snapshot",
            "snapshot": bridge_snapshot(
                snapshot_revision=2,
                sequence=1,
                execution_state="active",
            ),
        }
    )
    await eventually(lambda: runtime._execution_state is RuntimeExecutionState.ACTIVE)

    # An old reconnect history for the same session/epoch is still stale.  Its
    # lower snapshot revision and sequence cannot turn a known working run idle.
    peer.push(
        {
            "type": "history",
            "snapshot": bridge_snapshot(
                snapshot_revision=1,
                sequence=0,
                execution_state="idle",
            ),
            "events": [
                {
                    "name": "agent_settled",
                    "native_session_id": SESSION_A,
                    "bridge_epoch": 2,
                    "execution_state": "idle",
                    "sequence": 0,
                }
            ],
        }
    )
    await asyncio.sleep(0)

    assert runtime._execution_state is RuntimeExecutionState.ACTIVE
    assert runtime._snapshot_revision == 2
    assert runtime._last_sequence == 1
    await runtime.aclose()


async def test_pi_frontend_explicit_peer_reconnect_is_the_only_lower_epoch_reset() -> None:
    old_peer = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(
                bridge_epoch=8,
                snapshot_revision=9,
                sequence=12,
            )
        }
    )
    runtime = make_runtime(old_peer)
    await runtime.attach()

    replacement = ScriptedPiPeer(
        responses={
            "pi.snapshot": bridge_snapshot(
                session_id=SESSION_B,
                bridge_epoch=1,
                snapshot_revision=1,
                sequence=0,
            )
        }
    )
    snapshot = await runtime.reconnect(replacement)

    assert old_peer.closed is True
    assert snapshot.native_session_id == SESSION_B
    assert runtime._bridge_epoch == 1
    assert runtime._snapshot_revision == 1
    assert runtime._last_sequence == 0
    await runtime.aclose()
