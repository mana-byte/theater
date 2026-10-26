"""Codex native turn results, summary views, terminal evidence and bounded previews."""

from __future__ import annotations

from tests.support.codex_native_items import completed_item
from tests.support.codex_server import (
    ScriptedCodexServer,
    make_runtime,
    open_new,
    summary_turn_completed,
)
from tests.support.waiting import wait_until
from theater.harness.builtin.plugins.codex import runtime_constants as codex_runtime_constants
from theater.harness.builtin.plugins.codex.runtime_messages import _item_summary
from theater.harness.contracts.runtime import (
    HARNESS_RUNTIME_ERROR_MAX_CHARS,
    HARNESS_RUNTIME_RESULT_MAX_CHARS,
    ConnectionHealth,
    RuntimeNotification,
    SessionOpenMode,
)
from theater.models import Status
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus


def test_a_tool_item_summary_is_one_line_whatever_the_command() -> None:
    item = {"type": "commandExecution", "command": "/bin/zsh -lc 'cat <<EOF\n\thi\nEOF'"}

    assert _item_summary(item) == "codex item: commandExecution /bin/zsh -lc 'cat <<EOF hi EOF'"


async def test_result_at_contract_bound_stays_complete() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    bounded = "x" * HARNESS_RUNTIME_RESULT_MAX_CHARS
    server.push(
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "ui-thread-1",
                "turn": {
                    "id": "turn-bound",
                    "status": "completed",
                    "error": None,
                    "itemsView": "full",
                    "items": [{"id": "i-agent", "type": "agentMessage", "text": bounded}],
                },
            },
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 1, what="terminal outcome to land"
    )
    outcome = (await source.read()).terminal_evidence[0]
    assert len(outcome.result) == HARNESS_RUNTIME_RESULT_MAX_CHARS
    assert outcome.completeness.value == "complete"
    assert outcome.provenance.value == "native_evidence"
    await runtime.aclose()


async def test_result_over_contract_bound_is_bounded_and_partial() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    oversized = "x" * (HARNESS_RUNTIME_RESULT_MAX_CHARS + 1000)
    server.push(
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "ui-thread-1",
                "turn": {
                    "id": "turn-over",
                    "status": "completed",
                    "error": None,
                    "itemsView": "full",
                    "items": [{"id": "i-agent", "type": "agentMessage", "text": oversized}],
                },
            },
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 1, what="terminal outcome to land"
    )
    outcome = (await source.read()).terminal_evidence[0]
    # The stored result is bounded at the contract limit and its
    # completeness is downgraded — never COMPLETE for a truncated result.
    assert len(outcome.result) == HARNESS_RUNTIME_RESULT_MAX_CHARS
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    await runtime.aclose()


async def test_oversized_native_turn_error_is_bounded_not_fatal() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    oversized = "x" * (HARNESS_RUNTIME_ERROR_MAX_CHARS + 500)
    server.push(
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "ui-thread-1",
                "turn": {
                    "id": "turn-fail",
                    "status": "failed",
                    "error": {"message": oversized},
                    "itemsView": "full",
                    "items": [],
                },
            },
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 1, what="failed turn outcome to land"
    )
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.CONNECTED
    batch = await source.read()
    assert [outcome.native_turn_id for outcome in batch.terminal_evidence] == ["turn-fail"]
    outcome = batch.terminal_evidence[0]
    assert outcome.error is not None
    assert len(outcome.error) == HARNESS_RUNTIME_ERROR_MAX_CHARS
    assert outcome.error_code == "turn_failed"
    assert outcome.terminal.value == "failed"
    await runtime.aclose()


async def test_interrupted_turn_error_text_is_kept_without_a_failure_code() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "ui-thread-1",
                "turn": {
                    "id": "turn-int",
                    "status": "interrupted",
                    "error": {"message": "stopped by the user"},
                    "itemsView": "full",
                    "items": [],
                },
            },
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 1, what="interrupted outcome to land"
    )
    outcome = (await source.read()).terminal_evidence[0]
    assert outcome.terminal.value == "interrupted"
    assert outcome.error == "stopped by the user"
    assert outcome.error_code is None
    await runtime.aclose()


async def test_summary_view_final_agent_message_is_exact_complete_evidence() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        summary_turn_completed(
            items_view="summary",
            items=[{"id": "i-final", "type": "agentMessage", "text": "exact final answer"}],
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 1, what="summary outcome to land"
    )
    (outcome,) = (await source.read()).terminal_evidence
    assert outcome.native_turn_id == "turn-sum"
    assert outcome.terminal.value == "completed"
    assert outcome.result == "exact final answer"
    assert outcome.completeness.value == "complete"
    assert outcome.provenance.value == "native_evidence"
    await runtime.aclose()


async def test_summary_view_without_agent_message_invents_no_result() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    # A summary with no agentMessage text carries no final result: nothing is
    # promoted and no result is invented.
    server.push(summary_turn_completed(items_view="summary", items=[]))
    server.push(
        summary_turn_completed(
            turn_id="turn-user-only",
            items_view="summary",
            items=[
                {"id": "i-user", "type": "userMessage", "content": [{"type": "text", "text": "q"}]}
            ],
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 2, what="both summary outcomes to land"
    )
    outcomes = (await source.read()).terminal_evidence
    assert [outcome.result for outcome in outcomes] == [None, None]
    assert [outcome.completeness.value for outcome in outcomes] == [
        "unavailable",
        "unavailable",
    ]
    assert all(outcome.provenance.value == "native_evidence" for outcome in outcomes)
    await runtime.aclose()


async def test_not_loaded_view_is_never_promoted() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    # Stock failed/interrupted completions arrive as notLoaded with empty
    # items: no result is available and none is invented.
    server.push(
        summary_turn_completed(
            turn_id="turn-failed",
            status="failed",
            items_view="notLoaded",
            error={"message": "boom"},
        )
    )
    # A notLoaded view that nevertheless carries item text is not trusted as
    # the exact final message: no promotion.
    server.push(
        summary_turn_completed(
            turn_id="turn-notloaded-text",
            items_view="notLoaded",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "untrusted text"}],
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 2, what="both outcomes to land"
    )
    outcomes = (await source.read()).terminal_evidence
    assert [outcome.native_turn_id for outcome in outcomes] == [
        "turn-failed",
        "turn-notloaded-text",
    ]
    assert outcomes[0].result is None
    assert outcomes[0].completeness.value == "unavailable"
    assert outcomes[0].error == "boom"
    assert outcomes[1].result == "untrusted text"
    assert outcomes[1].completeness.value == "partial"
    assert outcomes[1].provenance.value == "live_stream"
    await runtime.aclose()


async def test_unknown_and_malformed_item_views_are_never_promoted() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        summary_turn_completed(
            turn_id="turn-unknown",
            items_view="everything",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "some text"}],
        )
    )
    server.push(
        summary_turn_completed(
            turn_id="turn-malformed",
            items_view=123,
            items=[{"id": "i-agent", "type": "agentMessage", "text": "more text"}],
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 2, what="both outcomes to land"
    )
    outcomes = (await source.read()).terminal_evidence
    # Unknown and malformed views are not the verified summary shape: no
    # promotion, the text stays partial live-stream evidence.
    assert [outcome.completeness.value for outcome in outcomes] == ["partial", "partial"]
    assert all(outcome.provenance.value == "live_stream" for outcome in outcomes)
    await runtime.aclose()


async def test_summary_view_failed_and_interrupted_semantics_stay_partial() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        summary_turn_completed(
            turn_id="turn-failed-sum",
            status="failed",
            items_view="summary",
            error={"message": "boom"},
            items=[{"id": "i-agent", "type": "agentMessage", "text": "partial text"}],
        )
    )
    server.push(
        summary_turn_completed(
            turn_id="turn-interrupted-sum",
            status="interrupted",
            items_view="summary",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "cut short"}],
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 2, what="both outcomes to land"
    )
    outcomes = (await source.read()).terminal_evidence
    assert [outcome.terminal.value for outcome in outcomes] == ["failed", "interrupted"]
    assert all(outcome.completeness.value == "partial" for outcome in outcomes)
    assert all(outcome.provenance.value == "live_stream" for outcome in outcomes)
    assert outcomes[0].error == "boom"
    await runtime.aclose()


async def test_oversized_summary_result_stays_bounded_and_partial() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    oversized = "x" * (HARNESS_RUNTIME_RESULT_MAX_CHARS + 500)
    server.push(
        summary_turn_completed(
            items_view="summary",
            items=[{"id": "i-final", "type": "agentMessage", "text": oversized}],
        )
    )
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 1, what="summary outcome to land"
    )
    (outcome,) = (await source.read()).terminal_evidence
    # The narrow promotion holds for the exact message, but the stored result is bounded at the
    # contract limit and downgraded — never COMPLETE for a truncated result.
    assert len(outcome.result) == HARNESS_RUNTIME_RESULT_MAX_CHARS
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    await runtime.aclose()


async def test_reconcile_summary_result_is_partial_evidence_once() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "th-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "error": None,
                        "itemsView": "summary",
                        "items": [
                            {
                                "id": "i-user",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "work"}],
                            },
                            {
                                "id": "i-agent",
                                "type": "agentMessage",
                                "text": "recovered final answer",
                            },
                        ],
                    }
                ],
            }
        },
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    source = runtime.live_source()
    (outcome,) = (await source.read()).terminal_evidence
    assert outcome.native_session_id == "th-1"
    assert outcome.native_turn_id == "turn-1"
    assert outcome.result == "recovered final answer"
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    # Exact-once: a live turn/completed replay for the same turn never
    # duplicates the reconciled terminal evidence.
    server.push(
        summary_turn_completed(
            thread_id="th-1",
            turn_id="turn-1",
            items_view="summary",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "recovered final answer"}],
        )
    )
    await wait_until(lambda: not server.pending, what="replayed completion consumed")
    replay = await source.read()
    assert replay.terminal_evidence == ()
    await runtime.aclose()


async def test_reconcile_summary_without_result_invents_nothing() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "th-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-empty",
                        "status": "completed",
                        "error": None,
                        "itemsView": "summary",
                        "items": [],
                    }
                ],
            }
        },
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    source = runtime.live_source()
    (outcome,) = (await source.read()).terminal_evidence
    # A reconciled summary with no agent message carries no result: nothing
    # is invented, and the outcome is recorded exactly once.
    assert outcome.native_turn_id == "turn-empty"
    assert outcome.result is None
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    server.push(
        summary_turn_completed(
            thread_id="th-1",
            turn_id="turn-empty",
            items_view="summary",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "late arrival"}],
        )
    )
    await wait_until(lambda: not server.pending, what="late completion consumed")
    replay = await source.read()
    assert replay.terminal_evidence == ()
    await runtime.aclose()


async def test_source_terminal_evidence_exact_and_once() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    turn_completed = RuntimeNotification(
        method="turn/completed",
        params={
            "threadId": "ui-thread-1",
            "turn": {
                "id": "turn-1",
                "status": "completed",
                "error": None,
                "itemsView": "full",
                "items": [{"id": "i-agent", "type": "agentMessage", "text": "final answer"}],
            },
        },
    )
    server.push(turn_completed)
    await wait_until(
        lambda: len(source.buffered_terminal_evidence()) == 1, what="terminal outcome to land"
    )
    batch = await source.read()
    assert len(batch.terminal_evidence) == 1
    outcome = batch.terminal_evidence[0]
    assert outcome.native_session_id == "ui-thread-1"
    assert outcome.native_turn_id == "turn-1"
    assert outcome.terminal.value == "completed"
    assert outcome.result == "final answer"
    assert outcome.completeness.value == "complete"
    assert outcome.provenance.value == "native_evidence"
    # Replayed turn/completed never repeats terminal evidence.
    server.push(turn_completed)
    await wait_until(lambda: not server.pending, what="replayed completion consumed")
    replay = await source.read()
    assert replay.terminal_evidence == ()
    await runtime.aclose()


async def test_status_broadcasts_never_create_terminal_evidence() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "ui-thread-1", "status": {"type": "active"}},
        )
    )
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "ui-thread-1", "status": {"type": "idle"}},
        )
    )
    await wait_until(lambda: runtime._status_hint is Status.IDLE, what="idle status broadcast")
    batch = await source.read()
    assert batch.terminal_evidence == ()
    assert batch.status is Status.IDLE
    assert batch.progressed is True
    await runtime.aclose()


async def test_delta_previews_are_coalesced_and_bounded() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    for chunk in ("alpha ", "beta ", "gamma"):
        server.push(
            RuntimeNotification(
                method="item/agentMessage/delta",
                params={
                    "threadId": "ui-thread-1",
                    "turnId": "turn-1",
                    "itemId": "i-live",
                    "delta": chunk,
                },
            )
        )
    server.push(
        RuntimeNotification(
            method="item/agentMessage/delta",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "i-live",
                "delta": "x" * 5000,
            },
        )
    )
    await wait_until(
        lambda: bool(runtime._preview_buffer._items), what="delta previews to accumulate"
    )
    batch = await source.read()
    previews = [fact for fact in batch.trajectory if fact.kind is TrajectoryKind.ASSISTANT]
    assert len(previews) == 1
    assert previews[0].status is TrajectoryStatus.RUNNING
    assert previews[0].native_id == "i-live"
    assert len(previews[0].summary) <= codex_runtime_constants.CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS
    # No further preview until new deltas arrive.
    quiet = await source.read()
    assert [fact for fact in quiet.trajectory if fact.kind is TrajectoryKind.ASSISTANT] == []
    await runtime.aclose()


async def test_delta_eviction_retains_reused_id_suppression_and_unfinished_previews() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()

    def delta(item_id: str, text: str) -> None:
        runtime._on_agent_message_delta(
            {"threadId": "ui-thread-1", "itemId": item_id, "delta": text}, None
        )

    async def previews() -> list:
        batch = await source.read()
        return [fact for fact in batch.trajectory if fact.kind is TrajectoryKind.ASSISTANT]

    delta("reused", "original")
    assert [fact.summary for fact in await previews()] == ["original"]
    for index in range(codex_runtime_constants.CODEX_RUNTIME_DELTA_ITEMS_MAX):
        delta(f"other-{index}", "x")
    assert [fact.native_id for fact in await previews()] == [
        f"other-{index}" for index in range(codex_runtime_constants.CODEX_RUNTIME_DELTA_ITEMS_MAX)
    ]
    assert source.health_snapshot()[0].dropped == 1

    # Eviction discards text but retains the last emitted length for this ID.
    delta("reused", "short")
    assert await previews() == []
    delta("reused", "again")
    assert [fact.summary for fact in await previews()] == ["shortagain"]

    delta("unfinished", "a")
    assert [fact.summary for fact in await previews()] == ["a"]
    assert await previews() == []
    delta("unfinished", "b")
    assert [fact.summary for fact in await previews()] == ["ab"]
    runtime._on_item_completed(
        {
            "threadId": "ui-thread-1",
            "item": {"id": "unfinished", "type": "agentMessage", "text": "ab"},
        },
        None,
    )
    delta("unfinished", "new")
    assert [fact.summary for fact in await previews()] == ["new"]
    await runtime.aclose()


async def test_native_cumulative_usage_never_becomes_response_usage() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="thread/tokenUsage/updated",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"last": {}, "total": {}},
            },
        )
    )
    server.push(completed_item("i-agent", item_type="agentMessage"))
    await wait_until(lambda: len(runtime._events) == 1, what="completed item to land")
    batch = await source.read()
    assert all(event.usage is None for event in batch.events)
    assert batch.terminal_evidence == ()
    await runtime.aclose()


async def test_tool_shaped_items_become_bounded_trajectory_facts() -> None:
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
                    "id": "i-cmd",
                    "type": "commandExecution",
                    "command": "/bin/zsh -lc 'ls'",
                },
            },
        )
    )
    await wait_until(lambda: len(runtime._facts) == 1, what="tool fact to land")
    batch = await source.read()
    tool_facts = [fact for fact in batch.trajectory if fact.kind is TrajectoryKind.TOOL_CALL]
    assert len(tool_facts) == 1
    assert tool_facts[0].native_id == "i-cmd"
    assert tool_facts[0].status is TrajectoryStatus.COMPLETED
    await runtime.aclose()


async def test_event_buffer_overflow_degrades_health_visibly() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    for index in range(codex_runtime_constants.CODEX_RUNTIME_EVENTS_BUFFER + 10):
        server.push(completed_item(f"i-{index}", item_type="agentMessage", text=f"m{index}"))
    await wait_until(
        lambda: source.health_snapshot()[0].dropped > 0,
        what="event buffer overflow to drop events",
    )
    await source.read()
    health = source.health_snapshot()[0]
    assert health.dropped > 0
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DEGRADED
    assert snapshot.health_diagnostics
    await runtime.aclose()


async def test_aclose_disconnects_only_and_backend_survives() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    await runtime.aclose()
    assert server.close_count == 1
    assert server.backend_alive is True
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    # Terminal evidence already recorded is still readable after disconnect.
    assert runtime.live_source() is not None
