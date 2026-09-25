"""Typing-only contract for Codex runtime concern mixins."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import asyncio
    from collections import OrderedDict, deque
    from collections.abc import Callable, Mapping, Sequence

    from theater.harness.contracts.events import Event
    from theater.harness.contracts.runtime import (
        CapabilityUnavailableReason,
        ConnectionHealth,
        ControlReceipt,
        NativeHumanInteraction,
        NativeTurnOutcome,
        NativeTurnTerminal,
        ResultCompleteness,
        ResultProvenance,
        RuntimeCapabilities,
        RuntimeConnection,
        RuntimeContext,
        RuntimeNotification,
        RuntimeRequestError,
        RuntimeSettings,
    )
    from theater.harness.contracts.source import Source
    from theater.models import Status

    class CodexRuntimeHost(Protocol):
        """Shared state and cross-concern methods supplied by ``CodexRuntime``."""

        context: RuntimeContext
        _connection: RuntimeConnection | None
        _receive_task: asyncio.Task[None] | None
        _history_reconcile_task: asyncio.Task[None] | None
        _subscription_recovery_task: asyncio.Task[None] | None
        _live_source: Source | None
        _native_session_id: str | None
        _active_turn_id: str | None
        _thread_status: str | None
        _pending_interaction: NativeHumanInteraction | None
        _settings: RuntimeSettings
        _settings_available: bool | None
        _settings_gate_reason: CapabilityUnavailableReason | None
        _subscribed: bool
        _health: ConnectionHealth
        _native_version: str | None
        _diagnostics: deque[str]
        _started_threads: deque[dict[str, object]]
        _thread_started_event: asyncio.Event
        _events: deque[Event]
        _facts: deque[object]
        _outcomes: asyncio.Queue[NativeTurnOutcome]
        _buffered_outcomes: dict[tuple[str, str], NativeTurnOutcome]
        _completed_items: OrderedDict[str, None]
        _terminal_turns: OrderedDict[tuple[str, str], object]
        _delta_items: OrderedDict[str, str]
        _delta_previewed_chars: dict[str, int]
        _status_hint: Status | None
        _activity_callback: Callable[[], None] | None
        _accepted: int
        _dropped: int

        async def _connect(self) -> None: ...
        async def _request(
            self, method: str, params: Mapping[str, object]
        ) -> Mapping[str, object]: ...
        def _require_connection(self) -> RuntimeConnection: ...
        def _require_session(self) -> str: ...
        async def _reconcile_resume_result(
            self, result: Mapping[str, object], expected: str
        ) -> None: ...
        async def _reconcile_thread(
            self,
            thread: Mapping[str, object],
            session: str,
            *,
            turns: Sequence[object],
        ) -> None: ...
        async def _subscribe_after_rollout(self) -> None: ...
        def _schedule_subscription_recovery(self) -> None: ...
        def _start_history_reconciliation(self, session: str) -> None: ...
        async def _probe_settings_gate(self) -> None: ...
        def _mark_settings_gate(self, error: RuntimeRequestError) -> None: ...
        async def _readback_settings(
            self, session: str, *, want_model: str | None, want_effort: str | None
        ) -> str | None: ...
        def _adopt_thread_settings(self, thread: Mapping[str, object]) -> bool: ...
        async def _handle_notification(self, notification: RuntimeNotification) -> None: ...
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
        ) -> None: ...
        def _notify_activity(self) -> None: ...
        def set_activity_callback(self, callback: Callable[[], None] | None) -> None: ...
        def _note_completed_item(self, item_id: str) -> None: ...
        def _push_event(self, event: Event) -> None: ...
        def _push_fact(self, fact: object) -> None: ...
        def _diagnostic(self, message: str) -> None: ...
        def _degrade(self, message: str) -> None: ...
        def _capabilities(self) -> RuntimeCapabilities: ...
        def _rejected(self, operation_id: str, code: str, message: str) -> ControlReceipt: ...
        def _unknown(
            self, operation_id: str, code: str, detail: str | None = None
        ) -> ControlReceipt: ...
        def _unknown_prompt_start(
            self, operation_id: str, code: str, detail: str | None = None
        ) -> ControlReceipt: ...

else:
    CodexRuntimeHost = object
