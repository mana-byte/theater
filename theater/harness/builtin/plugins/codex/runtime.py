"""The Codex native runtime: one participant's live app-server runtime."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from collections.abc import Callable

from theater.harness.contracts.events import Event
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ConnectionHealth,
    HarnessRuntime,
    NativeHumanInteraction,
    NativeTurnOutcome,
    RuntimeConnection,
    RuntimeContext,
    RuntimeSettings,
)
from theater.models import Status

from . import runtime_constants
from .live_source import CodexLiveSource
from .runtime_connection import CodexRuntimeConnection
from .runtime_controls import CodexRuntimeControls
from .runtime_events import CodexRuntimeEvents
from .runtime_history import CodexRuntimeHistory


class CodexRuntime(
    CodexRuntimeConnection,
    CodexRuntimeControls,
    CodexRuntimeHistory,
    CodexRuntimeEvents,
    HarnessRuntime,
):
    """One participant's live native Codex app-server runtime."""

    def __init__(self, context: RuntimeContext) -> None:
        self.context = context
        self._connection: RuntimeConnection | None = None
        self._receive_task: asyncio.Task[None] | None = None
        # A reconnect can page bounded historical turn summaries after the synchronous session
        # attach returns.
        self._history_reconcile_task: asyncio.Task[None] | None = None
        # One idle-broadcast subscription recovery attempt, held so the loop
        # cannot garbage-collect it mid-flight.
        self._subscription_recovery_task: asyncio.Task[None] | None = None
        self._live_source: CodexLiveSource | None = None
        self._native_session_id: str | None = None
        self._active_turn_id: str | None = None
        self._thread_status: str | None = None
        self._pending_interaction: NativeHumanInteraction | None = None
        self._settings = RuntimeSettings(
            model=context.model,
            reasoning_effort=context.reasoning_effort,
            supported_fields=runtime_constants._CODEX_SETTING_FIELDS,
        )
        self._settings_available: bool | None = None
        self._settings_gate_reason: CapabilityUnavailableReason | None = None
        self._subscribed = False
        self._health = ConnectionHealth.UNOPENED
        self._native_version: str | None = None
        self._diagnostics: deque[str] = deque(
            maxlen=runtime_constants.CODEX_RUNTIME_DIAGNOSTICS_MAX
        )
        # ---- UI-first NEW discovery ---------------------------------------
        self._started_threads: deque[dict[str, object]] = deque(maxlen=8)
        self._thread_started_event = asyncio.Event()
        # Events/facts may degrade on overflow; terminal evidence uses loss-free bounded
        # backpressure.
        self._events: deque[Event] = deque(maxlen=runtime_constants.CODEX_RUNTIME_EVENTS_BUFFER)
        self._facts: deque = deque(maxlen=runtime_constants.CODEX_RUNTIME_FACTS_BUFFER)
        self._outcomes: asyncio.Queue[NativeTurnOutcome] = asyncio.Queue(
            maxsize=runtime_constants.CODEX_RUNTIME_OUTCOMES_BUFFER
        )
        self._buffered_outcomes: dict[tuple[str, str], NativeTurnOutcome] = {}
        self._completed_items: OrderedDict[str, None] = OrderedDict()
        # Values are None once an outcome's enqueue committed, or the _PENDING_OUTCOME sentinel
        # while its bounded-queue insertion is still awaiting capacity.
        self._terminal_turns: OrderedDict[tuple[str, str], object] = OrderedDict()
        self._delta_items: OrderedDict[str, str] = OrderedDict()
        self._delta_previewed_chars: dict[str, int] = {}
        self._status_hint: Status | None = None
        # Coalesce arrival-driven observer wakes; never spawn a task per message.
        self._activity_callback: Callable[[], None] | None = None
        self._accepted = 0
        self._dropped = 0


def codex_runtime_factory(context: RuntimeContext) -> HarnessRuntime:
    """The manifest factory: one CodexRuntime per participant."""
    return CodexRuntime(context)


__all__ = [
    "CodexLiveSource",
    "CodexRuntime",
    "codex_runtime_factory",
]
