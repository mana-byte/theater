"""Codex native live source: item normalisation, activity wakeups and thread isolation."""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.support.codex_native_items import completed_item
from tests.support.codex_server import (
    ScriptedCodexServer,
    make_runtime,
    open_new,
)
from theater.harness.builtin.plugins.codex import runtime_constants as codex_runtime_constants
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.harness.builtin.plugins.codex.runtime_messages import _native_revision
from theater.harness.channels.hybrid import HybridSource
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    LiveChannelDeclaration,
    RuntimeNotification,
    SessionOpenMode,
)
from theater.harness.contracts.source import Batch, Source
from theater.models import Status


async def test_live_source_is_one_instance_for_the_runtime_lifetime() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    first = runtime.live_source()
    # Exactly one Source instance for the whole runtime lifetime.
    assert runtime.live_source() is first
    assert runtime.live_source() is first
    # One instance means one status cursor: drains never duplicate or steal.
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "ui-thread-1", "status": {"type": "active"}},
        )
    )
    await asyncio.sleep(0.02)
    batch = await first.read()
    assert batch.status is Status.WORKING
    # The cached source does not re-emit the same status transition; an
    # independent second instance would have reset the cursor and stolen it.
    quiet = await first.read()
    assert quiet.progressed is False
    await runtime.aclose()


async def test_cancelled_saturated_enqueue_rolls_back_dedupe_for_replay() -> None:
    from theater.harness.contracts.runtime import (
        NativeTurnTerminal,
        ResultCompleteness,
        ResultProvenance,
    )

    server = ScriptedCodexServer()
    server.respond(
        "thread/resume", {"thread": {"id": "th-1", "status": {"type": "idle"}, "turns": []}}
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    source = runtime.live_source()

    async def record(turn_id: str) -> None:
        await runtime._record_turn_outcome(
            "th-1",
            turn_id,
            NativeTurnTerminal.COMPLETED,
            result=None,
            completeness=ResultCompleteness.UNAVAILABLE,
            provenance=ResultProvenance.NATIVE_EVIDENCE,
            error=None,
        )

    # Saturate the bounded terminal-evidence queue.
    for index in range(codex_runtime_constants.CODEX_RUNTIME_OUTCOMES_BUFFER):
        await record(f"turn-{index}")
    # An insertion blocked on the full queue holds a pending dedupe key...
    blocked = asyncio.get_running_loop().create_task(record("turn-lost"))
    await asyncio.sleep(0.01)
    # ...so a concurrent insert of the same turn is deduped against it and
    # never double-inserts.
    duplicate = asyncio.get_running_loop().create_task(record("turn-lost"))
    await asyncio.sleep(0.01)
    assert duplicate.done() is True
    assert not duplicate.exception()
    # Cancelling the blocked insertion must roll the pending key back: the
    # outcome was never enqueued, so a replay may retry loss-free.
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    # Drain the saturated queue, then replay the same turn.
    buffered = source.buffered_terminal_evidence()
    drained = await source.read()
    assert buffered == drained.terminal_evidence
    assert source.buffered_terminal_evidence() == ()
    assert len(drained.terminal_evidence) == codex_runtime_constants.CODEX_RUNTIME_OUTCOMES_BUFFER
    await record("turn-lost")
    replayed = await source.read()
    assert [outcome.native_turn_id for outcome in replayed.terminal_evidence] == ["turn-lost"]
    # Exactly one "turn-lost" outcome exists across cancel, duplicate, and
    # replay: nothing was dropped and nothing was doubled.
    ids = [outcome.native_turn_id for outcome in drained.terminal_evidence]
    assert "turn-lost" not in ids
    await runtime.aclose()


async def test_source_normalizes_items_once_by_native_id() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(completed_item("i-user", item_type="userMessage", text="do it"))
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)
    batch = await source.read()
    assert [event.kind.value for event in batch.events] == ["user", "assistant"]
    assert [event.text for event in batch.events] == ["do it", "done"]
    assert batch.progressed is True
    # The backend replayed the same completed item: identity, not text,
    # decides; nothing is normalized a second time.
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert not replay.events
    await runtime.aclose()


async def test_item_started_then_deltas_then_completed_emits_exactly_once() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    # The captured sequence: item/started, deltas, then item/completed.
    server.push(
        RuntimeNotification(
            method="item/started",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "startedAtMs": 1000,
                "item": {"id": "i-agent", "type": "agentMessage"},
            },
        )
    )
    server.push(
        RuntimeNotification(
            method="item/agentMessage/delta",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "i-agent",
                "delta": "partial",
            },
        )
    )
    server.push(completed_item("i-agent", item_type="agentMessage", text="final answer"))
    await asyncio.sleep(0.05)
    batch = await source.read()
    # item/started must not mark the completion dedupe ledger: the completed
    # item is normalized exactly once...
    assistant = [event for event in batch.events if event.kind.value == "assistant"]
    assert [event.text for event in assistant] == ["final answer"]
    # ...and a replayed item/completed emits zero.
    server.push(completed_item("i-agent", item_type="agentMessage", text="final answer"))
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert [event for event in replay.events if event.kind.value == "assistant"] == []
    await runtime.aclose()


async def test_live_events_carry_native_identity_and_revision() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(completed_item("i-user", item_type="userMessage", text="do it"))
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)
    batch = await source.read()
    user, assistant = batch.events
    # Native item identity rides every completed-user/assistant event so the
    # composition reconciles live and durable records by identity.
    assert user.native_id == "i-user"
    assert assistant.native_id == "i-agent"
    # Anonymous default revision when the backend reports none.
    assert user.revision == 0
    assert assistant.revision == 0
    await runtime.aclose()


async def test_live_event_revision_comes_from_the_native_item() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "completedAtMs": 1234,
                "item": {
                    "id": "i-agent",
                    "type": "agentMessage",
                    "text": "done",
                    "revision": 7,
                },
            },
        )
    )
    await asyncio.sleep(0.05)
    batch = await source.read()
    assert batch.events[0].native_id == "i-agent"
    assert batch.events[0].revision == 7
    await runtime.aclose()


def test_native_revision_is_bounded_and_fails_closed() -> None:
    helper = _native_revision
    assert helper({}) == 0
    assert helper({"revision": -3}) == 0
    assert helper({"revision": "7"}) == 0
    assert helper({"revision": True}) == 0
    assert helper({"revision": 7}) == 7
    cap = codex_runtime_constants.CODEX_RUNTIME_REVISION_MAX
    assert helper({"revision": cap + 5}) == cap


async def test_activity_callback_fires_on_live_arrival_and_detaches() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    calls: list[str] = []
    source.set_activity_callback(lambda: calls.append("wake"))

    # A completed item arrives: the callback fires once, coalesced — no task
    # per message and no daemon import.
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)
    assert calls == ["wake"]

    # A live delta arrival wakes too.
    server.push(
        RuntimeNotification(
            method="item/agentMessage/delta",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "i-agent",
                "delta": "x",
            },
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake"]

    # Detach via None: further arrivals never call back.
    source.set_activity_callback(None)
    server.push(completed_item("i-user", item_type="userMessage", text="again"))
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake"]
    await runtime.aclose()


async def test_failed_activity_callback_degrades_health_and_detaches() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()

    def boom() -> None:
        raise RuntimeError("callback exploded")

    source.set_activity_callback(boom)
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)

    # The arrival still normalized; the broken wake hint degraded the health
    # visibly and detached itself instead of poisoning the receive loop.
    batch = await source.read()
    assert [event.native_id for event in batch.events] == ["i-agent"]
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DEGRADED
    server.push(completed_item("i-user", item_type="userMessage", text="later"))
    await asyncio.sleep(0.05)
    assert len((await source.read()).events) == 1
    await runtime.aclose()


def test_activity_callback_rejects_non_callable() -> None:
    server = ScriptedCodexServer()
    runtime = make_runtime(server)
    with pytest.raises(TypeError, match="callable"):
        runtime.set_activity_callback("not callable")  # type: ignore[arg-type]


async def test_state_only_arrivals_fire_the_activity_callback() -> None:
    """Status, approval, and resolution state is readable only after a wake."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    calls: list[str] = []
    source.set_activity_callback(lambda: calls.append("wake"))

    # thread/status/changed carries no event, fact, or outcome: the state
    # mutation itself must wake observation.
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "ui-thread-1", "status": {"type": "active"}},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake"]
    batch = await source.read()
    assert batch.status is Status.WORKING

    # A foreign thread's status never touches this runtime: no wake.
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "other-thread", "status": {"type": "idle"}},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake"]

    # A recorded approval request flips the readable status to
    # AWAITING_INPUT with no event or fact landing.
    server.push(
        RuntimeNotification(
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "reason": "Allow creating file?",
                "command": "/bin/zsh -lc 'touch /repo/x'",
            },
            request_id=0,
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake"]
    batch = await source.read()
    assert batch.status is Status.AWAITING_INPUT

    # Resolving the request clears the interaction: a state-only wake again.
    server.push(
        RuntimeNotification(
            method="serverRequest/resolved",
            params={"threadId": "ui-thread-1", "requestId": 0},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake", "wake"]
    batch = await source.read()
    assert batch.status is not Status.AWAITING_INPUT

    # An adopted settings update is readable capability state.
    server.push(
        RuntimeNotification(
            method="thread/settings/updated",
            params={
                "threadId": "ui-thread-1",
                "threadSettings": {"model": "gpt-5.6-sol", "reasoningEffort": "high"},
            },
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake", "wake", "wake"]

    # An ignored settings update (nothing adoptable) never wakes.
    server.push(
        RuntimeNotification(
            method="thread/settings/updated",
            params={"threadId": "ui-thread-1", "threadSettings": {}},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake", "wake", "wake"]
    await runtime.aclose()


async def test_turn_started_state_wakes_without_events() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    calls: list[str] = []
    source.set_activity_callback(lambda: calls.append("wake"))

    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-9"}},
        )
    )
    await asyncio.sleep(0.05)

    # The active-turn/status mutation is readable with no event or fact.
    assert calls == ["wake"]
    batch = await source.read()
    assert batch.status is Status.WORKING

    # A foreign turn never wakes.
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "other-thread", "turn": {"id": "turn-x"}},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake"]
    await runtime.aclose()


async def test_live_and_durable_item_events_reconcile_by_native_identity() -> None:
    """End to end: live normalization and the durable parser agree on identity."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    live = runtime.live_source()

    # Actual live normalization: one completed agent message item.
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)

    # Actual durable parser output for the same native item, from the
    # canonical paginated-rollout shape.
    durable_observer = CodexObserver()
    parsed = durable_observer.parse_record(
        json.dumps(
            {
                "timestamp": "2026-09-10T14:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "ui-thread-1",
                    "turn_id": "turn-1",
                    "item": {
                        "type": "AgentMessage",
                        "id": "i-agent",
                        "content": [{"type": "Text", "text": "done"}],
                    },
                },
            }
        ),
        0,
    )
    # The durable parser stamps the exact native item identity.
    assert [event.native_id for event in parsed.events] == ["i-agent"]
    assert parsed.events[0].kind.value == "assistant"

    class DurableRollout(Source):
        """Emits nothing on the first read, the parsed item on the second."""

        def __init__(self) -> None:
            self.reads = 0

        async def read(self) -> Batch:
            self.reads += 1
            if self.reads == 1:
                return Batch()
            return Batch(events=tuple(parsed.events))

    source = HybridSource(
        durable=DurableRollout(),
        live=live,
        live_channel=LiveChannelDeclaration(
            channel=ChannelDeclaration(
                id=codex_runtime_constants._LIVE_CHANNEL_ID, kind=ChannelKind.LIVE
            )
        ),
    )

    first = await source.read()
    assert [(event.native_id, event.text) for event in first.events] == [("i-agent", "done")]

    # The durable transcript caught up after live already emitted the item:
    # the same native id is suppressed, never emitted twice.
    second = await source.read()
    assert second.events == ()
    await runtime.aclose()


async def test_foreign_thread_payloads_never_leak_once_bound() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    foreign = [
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "other-thread", "status": {"type": "active"}},
        ),
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "other-thread", "turn": {"id": "t-other"}},
        ),
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "other-thread",
                "turn": {
                    "id": "t-other",
                    "status": "completed",
                    "itemsView": "full",
                    "items": [],
                },
            },
        ),
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "other-thread",
                "turnId": "t-other",
                "completedAtMs": 1,
                "item": {"id": "i-foreign", "type": "agentMessage", "text": "foreign text"},
            },
        ),
        RuntimeNotification(
            method="item/agentMessage/delta",
            params={
                "threadId": "other-thread",
                "turnId": "t-other",
                "itemId": "i-foreign",
                "delta": "foreign delta",
            },
        ),
        RuntimeNotification(
            method="thread/settings/updated",
            params={"threadId": "other-thread", "threadSettings": {"model": "foreign-model"}},
        ),
        RuntimeNotification(
            method="serverRequest/resolved",
            params={"threadId": "other-thread", "requestId": 0},
        ),
        RuntimeNotification(
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "other-thread",
                "turnId": "t-other",
                "itemId": "i-foreign",
                "reason": "foreign approval",
            },
            request_id=7,
        ),
        # threadId is required by the verified schema; a payload missing it is
        # malformed and exact identity is never guessed.
        RuntimeNotification(
            method="turn/completed",
            params={
                "turn": {
                    "id": "t-nothread",
                    "status": "completed",
                    "itemsView": "full",
                    "items": [],
                }
            },
        ),
    ]
    for notification in foreign:
        server.push(notification)
    await asyncio.sleep(0.05)
    snapshot = await runtime.snapshot()
    # No foreign status, interaction, item, active turn, settings, or
    # terminal evidence leaked into this runtime.
    assert snapshot.native_turn_id is None
    assert snapshot.pending_interaction is None
    assert snapshot.settings.model is None
    batch = await source.read()
    assert not batch.events
    assert batch.terminal_evidence == ()
    assert batch.status is None
    await runtime.aclose()


async def test_terminal_evidence_backpressure_is_loss_free() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume", {"thread": {"id": "th-1", "status": {"type": "idle"}, "turns": []}}
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    source = runtime.live_source()
    total = codex_runtime_constants.CODEX_RUNTIME_OUTCOMES_BUFFER + 10
    for index in range(total):
        server.push(
            RuntimeNotification(
                method="turn/completed",
                params={
                    "threadId": "th-1",
                    "turn": {
                        "id": f"turn-{index}",
                        "status": "completed",
                        "error": None,
                        "itemsView": "full",
                        "items": [],
                    },
                },
            )
        )
    # The receive loop blocks on the saturated queue and resumes only as the Source drains: every
    # exact terminal turn id is eventually emitted exactly once, in order, with nothing dropped.
    collected: list[str] = []
    for _ in range(4000):
        batch = await source.read()
        collected.extend(outcome.native_turn_id for outcome in batch.terminal_evidence)
        if len(collected) == total:
            break
        await asyncio.sleep(0.002)
    assert collected == [f"turn-{index}" for index in range(total)]
    # Connection processing resumed after the barrier: a later live item is
    # still normalized.
    server.push(
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "th-1",
                "turnId": "turn-after",
                "completedAtMs": 42,
                "item": {"id": "i-after", "type": "agentMessage", "text": "resumed"},
            },
        )
    )
    for _ in range(400):
        batch = await source.read()
        if any(event.text == "resumed" for event in batch.events):
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("connection processing did not resume after the backpressure barrier")
    # Backpressure dropped nothing: the channel never degraded.
    health = source.health_snapshot()[0]
    assert health.dropped == 0
    await runtime.aclose()
