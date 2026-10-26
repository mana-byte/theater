"""Codex native runtime approvals, questions and waiting flags."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.codex_server import (
    PARTICIPANT,
    ScriptedCodexServer,
    make_runtime,
    open_new,
)
from tests.support.waiting import wait_until
from theater.daemon.controls.activity import ActivityControls
from theater.daemon.controls.busy import BusyOperation
from theater.harness.builtin.plugins.codex import runtime_constants as codex_runtime_constants
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    NativeInteractionKind,
    RuntimeExecutionState,
    RuntimeNotification,
    SessionOpenMode,
)
from theater.models import AwaitingDecision, Status


async def test_approval_server_request_recorded_never_answered() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
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
            # The server assigns the id as integer 0; the captured type must be retained.
            request_id=0,
        )
    )
    await wait_until(lambda: 0 in runtime._pending_interactions, what="approval request recorded")
    snapshot = await runtime.snapshot()
    interaction = snapshot.pending_interaction
    assert interaction is not None
    assert interaction.kind.value == "approval"
    assert interaction.native_request_id == 0
    assert isinstance(interaction.native_request_id, int)
    assert interaction.native_turn_id == "turn-1"
    assert interaction.native_item_id == "item-1"
    assert "Allow creating" in interaction.details
    # The runtime never answers: the only notification ever sent is the
    # initialized handshake, and no decision request appears.
    assert [method for method, _ in server.notifications_sent] == ["initialized"]
    assert not any("decision" in method for method, _ in server.requests)
    # Resolution comes from the native side only.
    server.push(
        RuntimeNotification(
            method="serverRequest/resolved",
            params={"threadId": "ui-thread-1", "requestId": 0},
        )
    )
    await wait_until(
        lambda: 0 not in runtime._pending_interactions, what="resolution to clear the request"
    )
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is None
    await runtime.aclose()


async def test_blocking_request_user_input_sets_and_clears_awaiting_input() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="item/tool/requestUserInput",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "item-question",
                "isBlocking": True,
                "questions": [
                    {
                        "id": "scope",
                        "header": "Scope",
                        "question": "Which files should change?",
                        "options": [],
                    }
                ],
            },
            request_id=7,
        )
    )
    await wait_until(lambda: 7 in runtime._pending_interactions, what="blocking question recorded")

    snapshot = await runtime.snapshot()
    interaction = snapshot.pending_interaction
    assert interaction is not None
    assert interaction.kind.value == "clarification"
    assert interaction.native_request_id == 7
    assert interaction.native_turn_id == "turn-1"
    assert interaction.native_item_id == "item-question"
    assert interaction.details == "Which files should change?"
    assert (await source.read()).status is Status.AWAITING_INPUT

    server.push(
        RuntimeNotification(
            method="serverRequest/resolved",
            params={"threadId": "ui-thread-1", "requestId": 7},
        )
    )
    await wait_until(
        lambda: 7 not in runtime._pending_interactions, what="resolution to clear the question"
    )
    assert (await runtime.snapshot()).pending_interaction is None
    assert (await source.read()).status is not Status.AWAITING_INPUT
    await runtime.aclose()


async def test_nonblocking_request_user_input_keeps_working() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-1"}},
        )
    )
    server.push(
        RuntimeNotification(
            method="item/tool/requestUserInput",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "item-question",
                "isBlocking": False,
                "questions": [
                    {
                        "id": "optional",
                        "header": "Optional",
                        "question": "Anything else?",
                        "options": [],
                    }
                ],
            },
            request_id=8,
        )
    )
    await wait_until(
        lambda: not server.pending, what="turn start and non-blocking question consumed"
    )

    assert (await runtime.snapshot()).pending_interaction is None
    assert (await source.read()).status is Status.WORKING
    await runtime.aclose()


async def test_async_agent_message_questions_do_not_set_awaiting_input() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "completedAtMs": 1000,
                "item": {
                    "id": "item-q",
                    "type": "agentMessage",
                    "text": "Which option?",
                    "questions": [{"title": "Pick one", "options": ["a", "b"]}],
                },
            },
        )
    )
    await wait_until(lambda: len(runtime._events) == 1, what="agent message event to land")
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is None
    assert (await runtime.live_source().read()).events[-1].text == "Which option?"
    await runtime.aclose()


def approval_request(
    request_id: int, *, turn_id: str = "turn-1", reason: str = "Allow creating file?"
) -> RuntimeNotification:
    return RuntimeNotification(
        method="item/commandExecution/requestApproval",
        params={
            "threadId": "ui-thread-1",
            "turnId": turn_id,
            "itemId": f"item-{request_id}",
            "reason": reason,
        },
        request_id=request_id,
    )


def request_resolved(request_id: int, *, thread_id: str = "ui-thread-1") -> RuntimeNotification:
    return RuntimeNotification(
        method="serverRequest/resolved",
        params={"threadId": thread_id, "requestId": request_id},
    )


def status_changed(
    status: dict[str, object], *, thread_id: str = "ui-thread-1"
) -> RuntimeNotification:
    return RuntimeNotification(
        method="thread/status/changed",
        params={"threadId": thread_id, "status": status},
    )


async def test_concurrent_interactions_resolve_independently() -> None:
    """Resolving one concurrent request never drops a still-unresolved other."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(approval_request(0, reason="Allow creating file?"))
    server.push(approval_request(7, turn_id="turn-2", reason="Allow deleting file?"))
    await wait_until(
        lambda: set(runtime._pending_interactions) == {0, 7}, what="both approvals recorded"
    )

    # The earliest unresolved request stays visible; a newer one never hides it.
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 0
    assert "Allow creating" in snapshot.pending_interaction.details
    assert (await source.read()).status is Status.AWAITING_INPUT

    # Resolving B leaves A pending: the live source still reports waiting.
    server.push(request_resolved(7))
    await wait_until(lambda: 7 not in runtime._pending_interactions, what="resolution of 7")
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 0
    assert (await source.read()).status is Status.AWAITING_INPUT

    # A foreign thread's resolution never touches this runtime's requests.
    server.push(request_resolved(0, thread_id="other-thread"))
    await wait_until(lambda: not server.pending, what="foreign resolution consumed")
    assert (await runtime.snapshot()).pending_interaction is not None

    server.push(request_resolved(0))
    await wait_until(
        lambda: not runtime._pending_interactions, what="resolution to clear every request"
    )
    assert (await runtime.snapshot()).pending_interaction is None
    assert (await source.read()).status is not Status.AWAITING_INPUT
    await runtime.aclose()


async def test_replayed_unresolved_request_and_resolution_are_idempotent() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(approval_request(0, reason="first reason"))
    server.push(approval_request(0, reason="replayed reason"))
    await wait_until(
        lambda: (
            len(runtime._pending_interactions) == 1
            and "replayed reason" in runtime._pending_interactions[0].details
        ),
        what="replayed request deduped with refreshed details",
    )

    # The exact id deduplicates the replay; only the details refresh.
    assert len(runtime._pending_interactions) == 1
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 0
    assert "replayed reason" in snapshot.pending_interaction.details

    # Replayed resolutions and unknown ids are harmless no-ops.
    server.push(request_resolved(0))
    server.push(request_resolved(0))
    server.push(request_resolved(99))
    await wait_until(
        lambda: not runtime._pending_interactions, what="resolutions to clear the request"
    )
    assert (await runtime.snapshot()).pending_interaction is None
    await runtime.aclose()


async def test_waiting_flags_show_waiting_without_fabricating_a_request() -> None:
    """Flag-only waiting is honest snapshot evidence; no request id is invented."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()

    # Zero-rollout subscription gap: only the broadcast names the wait.
    server.push(status_changed({"type": "active", "activeFlags": ["waitingOnApproval"]}))
    await wait_until(
        lambda: runtime._waiting_approval_flag is True, what="waiting approval flag adopted"
    )
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.kind.value == "approval"
    assert snapshot.pending_interaction.native_request_id is None
    assert snapshot.pending_interaction.native_turn_id is None
    assert snapshot.pending_interaction.native_item_id is None
    assert "waitingOnApproval" in snapshot.pending_interaction.details
    assert (await source.read()).status is Status.AWAITING_INPUT

    # Malformed flags entries never manufacture absence: the wait stays.
    server.push(status_changed({"type": "active", "activeFlags": [None]}))
    await wait_until(lambda: not server.pending, what="malformed flags consumed")
    assert (await runtime.snapshot()).pending_interaction is not None
    assert (await source.read()).status is Status.AWAITING_INPUT

    # An active variant without any flags field is silence, not absence.
    server.push(status_changed({"type": "active"}))
    await wait_until(lambda: not server.pending, what="flagless active status consumed")
    assert (await source.read()).status is Status.AWAITING_INPUT

    # The idle variant carries no activeFlags; a valid idle clears the wait.
    server.push(status_changed({"type": "idle"}))
    await wait_until(
        lambda: runtime._pending_interaction_view() is None and runtime._status_hint is Status.IDLE,
        what="idle to clear the wait",
    )
    assert (await runtime.snapshot()).pending_interaction is None
    assert (await source.read()).status is Status.IDLE

    # The user-input flag waits the same way, as a clarification summary.
    server.push(status_changed({"type": "active", "activeFlags": ["waitingOnUserInput"]}))
    await wait_until(lambda: runtime._waiting_input_flag is True, what="waiting input flag adopted")
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.kind.value == "clarification"
    assert snapshot.pending_interaction.native_request_id is None
    assert (await source.read()).status is Status.AWAITING_INPUT

    # A well-formed no-waiting flags list is authoritative absence.
    server.push(status_changed({"type": "active", "activeFlags": []}))
    await wait_until(
        lambda: (
            runtime._pending_interaction_view() is None and runtime._status_hint is Status.WORKING
        ),
        what="empty flags to clear the wait",
    )
    assert (await runtime.snapshot()).pending_interaction is None
    assert (await source.read()).status is Status.WORKING
    await runtime.aclose()


async def test_flag_only_waiting_blocks_daemon_admission() -> None:
    """Honest flag-only evidence keeps daemon admission fail-closed."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(status_changed({"type": "active", "activeFlags": ["waitingOnApproval"]}))
    await wait_until(
        lambda: runtime._waiting_approval_flag is True, what="waiting approval flag adopted"
    )
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id is None
    assert ActivityControls._is_authoritatively_idle(snapshot) is False
    with pytest.raises(AwaitingDecision) as excinfo:
        ActivityControls()._reject_busy(PARTICIPANT, snapshot, operation=BusyOperation.SEND)
    assert "native approval" in str(excinfo.value)
    await runtime.aclose()


@pytest.mark.parametrize("flag", ["waitingOnApproval", "waitingOnUserInput"])
async def test_reconnect_recovers_waiting_flags_from_resume(flag: str) -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {"id": "ui-thread-1", "status": {"type": "active", "activeFlags": [flag]}},
        },
    )
    runtime = make_runtime(server)
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        pending = (await runtime.snapshot()).pending_interaction
        assert pending is not None
        assert pending.native_request_id is None
        assert flag in pending.details
        assert (await runtime.live_source().read()).status is Status.AWAITING_INPUT
    finally:
        await runtime.aclose()


async def test_late_resume_does_not_erase_newer_native_waiting() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    gate = asyncio.Event()
    entered = asyncio.Event()
    server.request_gates["thread/resume"] = gate
    server.request_entered["thread/resume"] = entered
    subscribe = asyncio.create_task(runtime._subscribe_after_rollout())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await runtime._handle_notification(
            status_changed({"type": "active", "activeFlags": ["waitingOnApproval"]})
        )
        gate.set()
        await subscribe
        snapshot = await runtime.snapshot()
        assert snapshot.pending_interaction is not None
        assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    finally:
        gate.set()
        await subscribe
        await runtime.aclose()


@pytest.mark.parametrize(
    ("status", "clears"),
    [
        # Unknown variants cannot assert absence, whatever their flags say.
        ({"type": "alien", "activeFlags": []}, False),
        # Malformed flags on a recognized variant are silence, not absence.
        ({"type": "idle", "activeFlags": [None]}, False),
        ({"type": "idle", "activeFlags": "waitingOnApproval"}, False),
        ({"type": "active", "activeFlags": ["alienFlag"]}, False),
        ({"type": "active"}, False),
        # Only coherent recognized shapes ever clear: valid idle without the
        # flags field, or a well-formed known-flags list on active.
        ({"type": "idle"}, True),
        ({"type": "active", "activeFlags": []}, True),
    ],
)
async def test_unknown_or_malformed_status_never_asserts_absence(
    status: dict[str, object], clears: bool
) -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(status_changed({"type": "active", "activeFlags": ["waitingOnApproval"]}))
    await wait_until(
        lambda: runtime._waiting_approval_flag is True, what="waiting approval flag adopted"
    )
    assert (await runtime.snapshot()).pending_interaction is not None
    server.push(status_changed(status))
    await wait_until(lambda: not server.pending, what="status broadcast consumed")
    pending = (await runtime.snapshot()).pending_interaction
    if clears:
        assert pending is None
    else:
        assert pending is not None
        assert pending.native_request_id is None
    await runtime.aclose()


async def test_status_evidence_never_clears_tracked_exact_requests() -> None:
    """Authoritative status clearing touches summaries, never exact requests."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(approval_request(0, turn_id="turn-1"))
    server.push(status_changed({"type": "active", "activeFlags": ["waitingOnApproval"]}))
    await wait_until(
        lambda: 0 in runtime._pending_interactions and runtime._waiting_approval_flag is True,
        what="approval request and waiting flag recorded",
    )
    server.push(status_changed({"type": "idle"}))
    await wait_until(
        lambda: 0 in runtime._pending_interactions and not runtime._waiting_approval_flag,
        what="idle to clear the flag but keep the exact request",
    )
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 0
    await runtime.aclose()


async def test_overflow_summary_reports_the_observed_kind() -> None:
    """A clarification-only overflow never claims an approval waited."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    bound = codex_runtime_constants.CODEX_RUNTIME_PENDING_INTERACTIONS_MAX
    for request_id in range(bound + 1):
        server.push(blocking_input_request(request_id))
    await wait_until(
        lambda: runtime._overflow_kinds == frozenset({NativeInteractionKind.CLARIFICATION}),
        what="overflow latch to report the clarification kind",
    )
    assert runtime._overflow_kinds == frozenset({NativeInteractionKind.CLARIFICATION})

    for request_id in range(bound):
        server.push(request_resolved(request_id))
    await wait_until(lambda: not runtime._pending_interactions, what="tracked requests resolved")
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.kind.value == "clarification"
    assert snapshot.pending_interaction.native_request_id is None
    assert ActivityControls._is_authoritatively_idle(snapshot) is False
    await runtime.aclose()


async def test_nonblocking_question_never_hides_a_blocking_approval() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(approval_request(0))
    server.push(
        RuntimeNotification(
            method="item/tool/requestUserInput",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "item-optional",
                "isBlocking": False,
                "questions": [{"id": "q", "question": "Anything else?", "options": []}],
            },
            request_id=8,
        )
    )
    await wait_until(
        lambda: 0 in runtime._pending_interactions and not server.pending,
        what="approval recorded and non-blocking question consumed",
    )

    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 0
    assert (await source.read()).status is Status.AWAITING_INPUT
    await runtime.aclose()


def blocking_input_request(request_id: int, *, turn_id: str = "turn-1") -> RuntimeNotification:
    return RuntimeNotification(
        method="item/tool/requestUserInput",
        params={
            "threadId": "ui-thread-1",
            "turnId": turn_id,
            "itemId": f"item-question-{request_id}",
            "isBlocking": True,
            "questions": [
                {"id": "scope", "header": "Scope", "question": "Which files?", "options": []}
            ],
        },
        request_id=request_id,
    )


def completed_turn(turn_id: str) -> RuntimeNotification:
    return RuntimeNotification(
        method="turn/completed",
        params={
            "threadId": "ui-thread-1",
            "turn": {"id": turn_id, "status": "completed", "itemsView": "full", "items": []},
        },
    )


async def test_new_turn_alone_never_clears_pending_clarification() -> None:
    """A new turn is not proof an earlier clarification resolved."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(blocking_input_request(2, turn_id="turn-2"))
    await wait_until(lambda: 2 in runtime._pending_interactions, what="blocking question recorded")

    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-3"}},
        )
    )
    await wait_until(lambda: runtime._active_turn_id == "turn-3", what="new turn to start")
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 2

    # Another turn's terminal evidence never touches this clarification.
    server.push(completed_turn("turn-9"))
    await wait_until(lambda: not server.pending, what="foreign terminal turn consumed")
    assert (await runtime.snapshot()).pending_interaction is not None

    # The asking turn's terminal evidence clears exactly its own request.
    server.push(completed_turn("turn-2"))
    await wait_until(
        lambda: 2 not in runtime._pending_interactions,
        what="own terminal turn to clear the question",
    )
    assert (await runtime.snapshot()).pending_interaction is None
    await runtime.aclose()


async def test_history_terminal_turn_clears_only_its_own_interactions() -> None:
    """Authoritative history is lifecycle evidence, live or reconciled."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(approval_request(1, turn_id="turn-1"))
    server.push(approval_request(2, turn_id="turn-2"))
    await wait_until(
        lambda: set(runtime._pending_interactions) == {1, 2}, what="both approvals recorded"
    )

    await runtime._reconcile_thread(
        {"id": "ui-thread-1", "status": {"type": "idle"}},
        "ui-thread-1",
        turns=[{"id": "turn-1", "status": "completed", "items": []}],
    )
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 2
    await runtime.aclose()


async def test_approval_outranks_clarification_and_summaries_in_the_view() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(blocking_input_request(3))
    server.push(approval_request(4))
    await wait_until(
        lambda: set(runtime._pending_interactions) == {3, 4},
        what="clarification and approval recorded",
    )

    # An exact approval outranks an earlier clarification.
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 4
    assert snapshot.pending_interaction.kind.value == "approval"

    # An equivalent flag summary never displaces the exact request.
    server.push(status_changed({"type": "active", "activeFlags": ["waitingOnApproval"]}))
    await wait_until(
        lambda: runtime._waiting_approval_flag is True, what="waiting approval flag adopted"
    )
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction.native_request_id == 4

    # With the approval resolved, the flag still evidences an unknown approval
    # waiting, which outranks the exact clarification.
    server.push(request_resolved(4))
    await wait_until(lambda: 4 not in runtime._pending_interactions, what="approval resolved")
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id is None
    assert snapshot.pending_interaction.kind.value == "approval"

    # Only once the flag clears does the exact clarification surface.
    server.push(status_changed({"type": "active", "activeFlags": []}))
    await wait_until(
        lambda: not (runtime._waiting_approval_flag or runtime._waiting_input_flag),
        what="waiting flags cleared",
    )
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 3
    assert snapshot.pending_interaction.kind.value == "clarification"
    await runtime.aclose()


async def test_terminal_turn_clears_only_its_own_interactions() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(approval_request(0, turn_id="turn-1"))
    server.push(approval_request(1, turn_id="turn-2"))
    await wait_until(
        lambda: set(runtime._pending_interactions) == {0, 1}, what="both approvals recorded"
    )
    assert (await runtime.snapshot()).pending_interaction is not None

    # Turn 2's terminal ends only turn 2's request; turn 1's survives.
    server.push(completed_turn("turn-2"))
    await wait_until(
        lambda: 1 not in runtime._pending_interactions and 0 in runtime._pending_interactions,
        what="turn 2 terminal to clear only its request",
    )
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 0

    server.push(completed_turn("turn-1"))
    await wait_until(
        lambda: not runtime._pending_interactions, what="turn 1 terminal to clear its request"
    )
    assert (await runtime.snapshot()).pending_interaction is None
    await runtime.aclose()


async def test_pending_interaction_bound_never_evicts_into_a_safe_snapshot() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    bound = codex_runtime_constants.CODEX_RUNTIME_PENDING_INTERACTIONS_MAX
    for request_id in range(bound + 1):
        server.push(approval_request(request_id))
    await wait_until(
        lambda: (
            len(runtime._pending_interactions) == bound
            and runtime._overflow_kinds == frozenset({NativeInteractionKind.APPROVAL})
        ),
        what="bounded requests tracked with the overflow latch",
    )

    # The bound holds; the tracked exact request stays visible.
    assert len(runtime._pending_interactions) == bound
    assert runtime._overflow_kinds == frozenset({NativeInteractionKind.APPROVAL})
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DEGRADED
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id == 0
    assert (await source.read()).status is Status.AWAITING_INPUT

    # Resolving every tracked request keeps admission closed for the untracked
    # one, without ever claiming a fake exact id.
    for request_id in range(bound):
        server.push(request_resolved(request_id))
    await wait_until(lambda: not runtime._pending_interactions, what="tracked requests resolved")
    assert runtime._pending_interactions == {}
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.native_request_id is None
    assert ActivityControls._is_authoritatively_idle(snapshot) is False
    assert (await source.read()).status is Status.AWAITING_INPUT

    # Only authoritative no-waiting evidence ends the latched wait.
    server.push(status_changed({"type": "idle"}))
    await wait_until(
        lambda: runtime._overflow_kinds == frozenset() and runtime._status_hint is Status.IDLE,
        what="idle to clear the overflow latch",
    )
    assert (await source.read()).status is Status.IDLE
    assert runtime._overflow_kinds == frozenset()

    # With the latch cleared, tracking works normally again.
    server.push(approval_request(bound + 1))
    await wait_until(
        lambda: len(runtime._pending_interactions) == 1,
        what="new request tracked after the latch cleared",
    )
    assert len(runtime._pending_interactions) == 1
    await runtime.aclose()
