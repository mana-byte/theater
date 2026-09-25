"""Codex native notification and event normalization."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping

from theater.harness.contracts.events import Event, EventKind, clip
from theater.harness.contracts.runtime import (
    HARNESS_RUNTIME_ERROR_MAX_CHARS,
    HARNESS_RUNTIME_RESULT_MAX_CHARS,
    ConnectionHealth,
    NativeHumanInteraction,
    NativeInteractionKind,
    NativeRequestId,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeNotification,
    validate_native_request_id,
)
from theater.models import Status
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus

from ._runtime_host import CodexRuntimeHost
from .runtime_constants import (
    _APPROVAL_METHOD_SUFFIX,
    _CLARIFICATION_METHOD_MARKERS,
    _PENDING_OUTCOME,
    _REQUEST_USER_INPUT_METHOD,
    _TERMINAL_BY_STATUS,
    CODEX_RUNTIME_COMPLETED_ITEMS_MAX,
    CODEX_RUNTIME_DELTA_ITEMS_MAX,
    CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS,
    CODEX_RUNTIME_TERMINAL_TURNS_MAX,
)
from .runtime_messages import (
    _agent_message_text,
    _bounded_str,
    _clarification_details,
    _completed_at,
    _fact,
    _item_summary,
    _native_revision,
    _seconds_from_ms,
    _summary_view_carries_exact_final_message,
    _thread_id_of,
    _turn_error_message,
    _user_message_text,
)


class CodexRuntimeEvents(CodexRuntimeHost):
    _thread_status: str | None
    _active_turn_id: str | None
    _pending_interaction: NativeHumanInteraction | None
    _status_hint: Status | None

    async def _handle_notification(self, notification: RuntimeNotification) -> None:
        method = notification.method
        params = notification.params
        handler = _NOTIFICATION_HANDLERS.get(method)
        if handler is not None:
            # Handlers that record terminal evidence return a coroutine whose bounded-queue
            # insertion may await the Source's drain — real backpressure instead of loss.
            outcome = handler(self, params, notification.request_id)
            if asyncio.iscoroutine(outcome):
                await outcome
            return
        if notification.request_id is not None:
            self._record_server_request(method, params, notification.request_id)
            return
        if method == "error":
            message = _bounded_str(params.get("message"), limit=2000) or method
            self._push_event(Event(kind=EventKind.ERROR, text=clip(message)))
            self._diagnostic(f"native error notification: {message[:200]}")

    def _thread_filter(self, params: Mapping[str, object]) -> bool:
        """Exact ``threadId`` matching once the runtime is bound."""
        if self._native_session_id is None:
            return True
        return params.get("threadId") == self._native_session_id

    def _on_thread_started(self, params: Mapping[str, object], _: NativeRequestId | None) -> None:
        thread = params.get("thread")
        if not isinstance(thread, Mapping):
            return
        if self._native_session_id is not None:
            # Extra threads (e.g. the TUI's ephemeral title-generation thread)
            # never rebind identity; record them for diagnostics only.
            self._diagnostic(
                f"additional thread/started ignored: {_thread_id_of(thread) or 'unknown id'}"
            )
            return
        self._started_threads.append(dict(thread))
        self._thread_started_event.set()

    def _on_thread_status_changed(
        self, params: Mapping[str, object], _: NativeRequestId | None
    ) -> None:
        status = params.get("status")
        status_type = status.get("type") if isinstance(status, Mapping) else None
        if not isinstance(status_type, str):
            return
        if not self._thread_filter(params):
            # Foreign-thread status never touches this runtime's snapshot.
            return
        self._thread_status = status_type
        # A status broadcast is never terminal evidence; it only updates the
        # live status snapshot a source may report.
        if status_type == "active":
            self._status_hint = Status.WORKING
        elif status_type == "idle":
            # A fresh exact native idle status supersedes any turn id cached from an earlier active
            # view.
            self._active_turn_id = None
            self._status_hint = Status.IDLE
            # A send-time subscription may have been deferred by the first-turn
            # rollout race; the idle broadcast is the bounded recovery point.
            self._schedule_subscription_recovery()
        # The new status is readable without any event or fact landing, so
        # the state change itself must wake observation.
        self._notify_activity()

    def _on_turn_started(self, params: Mapping[str, object], _: NativeRequestId | None) -> None:
        if not self._thread_filter(params):
            return
        turn = params.get("turn")
        turn_id = _bounded_str(turn.get("id") if isinstance(turn, Mapping) else None, limit=512)
        if turn_id is None:
            return
        self._active_turn_id = turn_id
        self._thread_status = "active"
        self._status_hint = Status.WORKING
        # A new turn supersedes a pending clarification the human answered by
        # typing; approval requests clear only via serverRequest/resolved.
        interaction = self._pending_interaction
        if interaction is not None and interaction.kind is NativeInteractionKind.CLARIFICATION:
            self._pending_interaction = None
        # The turn/state mutations are readable without any event or fact.
        self._notify_activity()

    async def _on_turn_completed(
        self, params: Mapping[str, object], _: NativeRequestId | None
    ) -> None:
        if not self._thread_filter(params):
            # A foreign thread's completion must never fabricate an outcome
            # for this runtime's session.
            return
        turn = params.get("turn")
        if not isinstance(turn, Mapping):
            return
        session = self._native_session_id
        turn_id = _bounded_str(turn.get("id"), limit=512)
        status = turn.get("status")
        if session is None or turn_id is None or not isinstance(status, str):
            return
        terminal = _TERMINAL_BY_STATUS.get(status)
        if terminal is None:
            return
        items = turn.get("items")
        items_view = turn.get("itemsView")
        result_text = _agent_message_text(items)
        if result_text is not None and (
            items_view in (None, "full")
            or _summary_view_carries_exact_final_message(terminal, items_view)
        ):
            # Promote only full history or the verified live summary's exact final agent message.
            completeness = ResultCompleteness.COMPLETE
            provenance = ResultProvenance.NATIVE_EVIDENCE
        elif result_text is not None:
            completeness = ResultCompleteness.PARTIAL
            provenance = ResultProvenance.LIVE_STREAM
        else:
            completeness = ResultCompleteness.UNAVAILABLE
            provenance = ResultProvenance.NATIVE_EVIDENCE
        await self._record_turn_outcome(
            session,
            turn_id,
            terminal,
            result=result_text,
            completeness=completeness,
            provenance=provenance,
            error=_turn_error_message(turn.get("error")),
            completed_at=_completed_at(turn),
        )
        if self._active_turn_id == turn_id:
            self._active_turn_id = None

    def _on_agent_message_delta(
        self, params: Mapping[str, object], _: NativeRequestId | None
    ) -> None:
        if not self._thread_filter(params):
            return
        item_id = _bounded_str(params.get("itemId"), limit=512)
        delta = params.get("delta")
        if item_id is None or not isinstance(delta, str):
            return
        buffer = self._delta_items.get(item_id)
        if buffer is None:
            if len(self._delta_items) >= CODEX_RUNTIME_DELTA_ITEMS_MAX:
                self._delta_items.popitem(last=False)
                self._dropped += 1
            buffer = ""
        if len(buffer) >= CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS:
            return
        buffer += delta
        if len(buffer) > CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS:
            buffer = buffer[:CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS]
        self._delta_items[item_id] = buffer
        self._notify_activity()

    def _on_item_completed(self, params: Mapping[str, object], _: NativeRequestId | None) -> None:
        if not self._thread_filter(params):
            return
        item = params.get("item")
        if not isinstance(item, Mapping):
            return
        item_id = _bounded_str(item.get("id"), limit=512)
        if item_id is None:
            return
        # Native item identity, not text equality: a completed item is normalized exactly once,
        # whatever the backend replays.
        if item_id in self._completed_items:
            return
        self._note_completed_item(item_id)
        turn_id = _bounded_str(params.get("turnId"), limit=512)
        timestamp = _seconds_from_ms(params.get("completedAtMs"))
        item_type = item.get("type")
        if item_type == "userMessage":
            text = _user_message_text(item.get("content"))
            self._push_event(
                Event(
                    kind=EventKind.USER,
                    text=clip(text),
                    turn_id=turn_id,
                    ts=timestamp,
                    native_id=item_id,
                    revision=_native_revision(item),
                )
            )
            return
        if item_type == "agentMessage":
            raw_text = item.get("text")
            text = raw_text if isinstance(raw_text, str) else ""
            self._push_event(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=clip(text),
                    turn_id=turn_id,
                    ts=timestamp,
                    native_id=item_id,
                    revision=_native_revision(item),
                )
            )
            self._delta_items.pop(item_id, None)
            self._delta_previewed_chars.pop(item_id, None)
            return
        # Tool-shaped and unknown items are normalized as bounded trajectory
        # facts; the durable parser remains authoritative for history.
        summary = _item_summary(item)
        if summary is None:
            return
        self._push_fact(
            _fact(
                kind=TrajectoryKind.TOOL_CALL,
                summary=summary,
                native_id=item_id,
                turn_id=turn_id,
                status=TrajectoryStatus.COMPLETED,
            )
        )

    def _on_settings_updated(self, params: Mapping[str, object], _: NativeRequestId | None) -> None:
        if not self._thread_filter(params):
            return
        settings = params.get("threadSettings")
        if isinstance(settings, Mapping) and self._adopt_thread_settings(settings):
            # Adopted settings are readable state with no event or fact.
            self._notify_activity()

    def _on_server_request_resolved(
        self, params: Mapping[str, object], _: NativeRequestId | None
    ) -> None:
        if not self._thread_filter(params):
            return
        request_id = params.get("requestId")
        interaction = self._pending_interaction
        if interaction is None:
            return
        if interaction.native_request_id == request_id:
            self._pending_interaction = None
            # Clearing a pending interaction changes the readable status
            # snapshot (no more AWAITING_INPUT) without any event landing.
            self._notify_activity()

    def _record_server_request(
        self, method: str, params: Mapping[str, object], request_id: NativeRequestId
    ) -> None:
        """Observe one native server request; never send an answer."""
        if not self._thread_filter(params):
            return
        validate_native_request_id(request_id, "server request id")
        if method == _REQUEST_USER_INPUT_METHOD and params.get("isBlocking") is False:
            return
        if method.endswith(_APPROVAL_METHOD_SUFFIX):
            kind = NativeInteractionKind.APPROVAL
        elif any(marker in method for marker in _CLARIFICATION_METHOD_MARKERS):
            kind = NativeInteractionKind.CLARIFICATION
        else:
            self._diagnostic(f"observed unclassified server request {method}")
            return
        details = params.get("reason")
        if not isinstance(details, str) or not details:
            questions = params.get("questions")
            details = (
                _clarification_details(questions)
                if isinstance(questions, (list, tuple)) and questions
                else method
            )
        self._pending_interaction = NativeHumanInteraction(
            kind=kind,
            native_request_id=request_id,
            native_turn_id=_bounded_str(params.get("turnId"), limit=512),
            native_item_id=_bounded_str(params.get("itemId"), limit=512),
            details=details[:240],
        )
        # A recorded approval/clarification flips the readable status to
        # AWAITING_INPUT with no event or fact landing; wake observation.
        self._notify_activity()

    async def _record_turn_outcome(
        self,
        session: str,
        turn_id: str,
        terminal: NativeTurnTerminal,
        *,
        result: str | None,
        completeness: ResultCompleteness,
        provenance: ResultProvenance,
        error: str | None,
        from_history: bool = False,
        completed_at: float | None = None,
    ) -> None:
        key = (session, turn_id)
        if key in self._terminal_turns:
            # A pending (in-flight) or committed insert for this exact turn
            # never inserts a second outcome, whichever paths race here.
            return
        self._terminal_turns[key] = _PENDING_OUTCOME
        if len(self._terminal_turns) > CODEX_RUNTIME_TERMINAL_TURNS_MAX:
            self._terminal_turns.popitem(last=False)
        # Truncated results must remain PARTIAL; trajectory previews have a separate limit.
        result_text = result
        completeness_final = completeness
        if result is not None and len(result) > HARNESS_RUNTIME_RESULT_MAX_CHARS:
            result_text = result[:HARNESS_RUNTIME_RESULT_MAX_CHARS]
            completeness_final = ResultCompleteness.PARTIAL
        # Bound untrusted errors before contract validation so oversized text cannot disconnect the
        # receiver.
        error_text = None if error is None else error[:HARNESS_RUNTIME_ERROR_MAX_CHARS]
        outcome = NativeTurnOutcome(
            native_session_id=session,
            native_turn_id=turn_id,
            terminal=terminal,
            result=result_text,
            completeness=completeness_final,
            provenance=provenance,
            error_code=None if error is None else "turn_failed",
            error=error_text,
            from_history=from_history,
            completed_at=completed_at,
        )
        # Bounded with real backpressure: a full queue awaits the live Source's cooperative drain —
        # terminal evidence is never silently discarded.
        try:
            await self._outcomes.put(outcome)
        except BaseException:
            # The dedupe key commits only with a successful enqueue: a cancellation while awaiting
            # queue capacity must not strand a key that later replays would be deduped against.
            if self._terminal_turns.get(key) is _PENDING_OUTCOME:
                del self._terminal_turns[key]
            raise
        self._terminal_turns[key] = None
        self._buffered_outcomes[key] = outcome
        self._notify_activity()

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        """Install or detach the optional arrival-driven wake hook."""
        if callback is not None and not callable(callback):
            raise TypeError("activity callback must be callable or None")
        self._activity_callback = callback

    def _notify_activity(self) -> None:
        callback = self._activity_callback
        if callback is None:
            return
        try:
            callback()
        except Exception:
            self._degrade("live activity callback failed")
            self._activity_callback = None

    def _note_completed_item(self, item_id: str) -> None:
        self._completed_items[item_id] = None
        if len(self._completed_items) > CODEX_RUNTIME_COMPLETED_ITEMS_MAX:
            self._completed_items.popitem(last=False)

    def _push_event(self, event: Event) -> None:
        if len(self._events) == self._events.maxlen:
            self._dropped += 1
            self._degrade("live event buffer saturated; oldest events dropped")
        self._events.append(event)
        self._accepted += 1
        self._notify_activity()

    def _push_fact(self, fact: object) -> None:
        if len(self._facts) == self._facts.maxlen:
            self._dropped += 1
            self._degrade("live fact buffer saturated; oldest facts dropped")
        self._facts.append(fact)
        self._accepted += 1
        self._notify_activity()

    def _diagnostic(self, message: str) -> None:
        self._diagnostics.append(message[:240])

    def _degrade(self, message: str) -> None:
        self._health = ConnectionHealth.DEGRADED
        self._diagnostic(message)


_NOTIFICATION_HANDLERS: dict = {
    "thread/started": CodexRuntimeEvents._on_thread_started,
    "thread/status/changed": CodexRuntimeEvents._on_thread_status_changed,
    "turn/started": CodexRuntimeEvents._on_turn_started,
    "turn/completed": CodexRuntimeEvents._on_turn_completed,
    # Only item completions enter the ledger; marking item/started would suppress later completion.
    "item/agentMessage/delta": CodexRuntimeEvents._on_agent_message_delta,
    "item/completed": CodexRuntimeEvents._on_item_completed,
    "thread/settings/updated": CodexRuntimeEvents._on_settings_updated,
    "serverRequest/resolved": CodexRuntimeEvents._on_server_request_resolved,
}
