"""The Codex native runtime: one participant's live app-server runtime."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Mapping, Sequence

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.events import Event, EventKind, clip
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.runtime import (
    HARNESS_RUNTIME_ERROR_MAX_CHARS,
    HARNESS_RUNTIME_RESULT_MAX_CHARS,
    CapabilityUnavailableReason,
    ConnectionHealth,
    ControlReceipt,
    DeliveryResult,
    HarnessRuntime,
    NativeHumanInteraction,
    NativeInteractionKind,
    NativeRequestId,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeBinding,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeConnection,
    RuntimeConnectionClosed,
    RuntimeConnectionError,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeLifecyclePhase,
    RuntimeNotification,
    RuntimeRequestError,
    RuntimeRequestTimeout,
    RuntimeSettings,
    RuntimeSnapshot,
    RuntimeWiring,
    SessionOpenMode,
    validate_native_request_id,
)
from theater.harness.contracts.source import BATCH_TERMINAL_EVIDENCE_MAX, Batch, Source
from theater.models import Status
from theater.trajectory.content import ContentPreview
from theater.trajectory.enums import TrajectoryKind, TrajectoryLane, TrajectoryStatus

from .runtime_plan import (
    CODEX_RUNTIME_COMPATIBILITY_POLICY,
    CODEX_RUNTIME_VERIFIED_VERSIONS,
    plan_codex_frontend,
)

logger = logging.getLogger("theater.harness.codex.runtime")

#: Default deadlines from the approved plan (§3.5): 30 s startup, 10 s control.
CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS = 30.0
CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS = 10.0

#: Bounded normalization state.
CODEX_RUNTIME_EVENTS_BUFFER = 256
CODEX_RUNTIME_FACTS_BUFFER = 256
CODEX_RUNTIME_OUTCOMES_BUFFER = BATCH_TERMINAL_EVIDENCE_MAX
CODEX_RUNTIME_EVENTS_PER_BATCH = 64
#: Only completions mark the normalized-item ledger; ``item/started`` never
#: does, or the normal started → deltas → completed sequence would be dropped.
CODEX_RUNTIME_COMPLETED_ITEMS_MAX = 1024
CODEX_RUNTIME_TERMINAL_TURNS_MAX = 1024
CODEX_RUNTIME_DELTA_ITEMS_MAX = 32
CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS = 2000
#: The synchronous ``thread/resume`` view remains a tiny current-state aid, not history recovery.
CODEX_RUNTIME_RECONCILE_TURNS = 2
#: One paginated history request processes at most this many turn summaries.
CODEX_RUNTIME_RECONCILE_PAGE_SIZE = 16
#: Bound work per pass, not the lifetime of an accepted turn's recovery.
#: The owned task retains its cursor across passes and backs off on failures.
CODEX_RUNTIME_RECONCILE_MAX_PAGES = 64
CODEX_RUNTIME_RECONCILE_PAUSE_SECONDS = 0.05
CODEX_RUNTIME_RECONCILE_RETRY_SECONDS = 0.5
CODEX_RUNTIME_RECONCILE_MAX_RETRY_SECONDS = 5.0
CODEX_RUNTIME_DIAGNOSTICS_MAX = 8
#: Native item revisions are small monotonic counters; anything beyond this
#: bound is treated as the anonymous default rather than trusted as identity.
CODEX_RUNTIME_REVISION_MAX = 1_000_000_000

_LIVE_CHANNEL_ID = "native-live"

#: Deduplicate pending outcomes, committing only after enqueue; cancellation permits loss-free
#: replay.
_PENDING_OUTCOME = object()

_APPROVAL_METHOD_SUFFIX = "requestApproval"
_CLARIFICATION_METHOD_MARKERS = ("requestUserInput", "elicitation")

_TERMINAL_BY_STATUS = {
    "completed": NativeTurnTerminal.COMPLETED,
    "interrupted": NativeTurnTerminal.INTERRUPTED,
    "failed": NativeTurnTerminal.FAILED,
}

_initialize_params: dict[str, object] = {
    "clientInfo": {"name": "theater", "title": "Theater", "version": "1.0"},
    # thread/settings/update is experimental and capability-gated; request the
    # capability up front so the gate is honest per backend, never presumed.
    "capabilities": {"experimentalApi": True},
}


def _bounded_str(value: object, *, limit: int) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        return None
    return value


def _thread_id_of(thread: object) -> str | None:
    if not isinstance(thread, Mapping):
        return None
    return _bounded_str(thread.get("id"), limit=512)


def _resume_params(session: str) -> dict[str, object]:
    # initialTurnsPage is a *separate* response field. It does not disable
    # full thread.turns hydration; excludeTurns is essential on stock 0.154.
    return {
        "threadId": session,
        "excludeTurns": True,
        "initialTurnsPage": {
            "limit": CODEX_RUNTIME_RECONCILE_TURNS,
            "itemsView": "summary",
            "sortDirection": "desc",
        },
    }


def _completed_at(turn: Mapping[str, object]) -> float | None:
    value = turn.get("completedAt")
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 0 <= value <= 253402300799
        and math.isfinite(value)
    ):
        return float(value)
    return None


def _history_turns(result: Mapping[str, object]) -> Sequence[object]:
    data = result.get("data")
    if not isinstance(data, (list, tuple)):
        raise RuntimeConnectionError("thread/turns/list returned no usable turn page")
    if len(data) > CODEX_RUNTIME_RECONCILE_PAGE_SIZE:
        raise RuntimeConnectionError("thread/turns/list exceeded the requested page bound")
    return data


class CodexRuntime(HarnessRuntime):
    """One participant's live native Codex app-server runtime."""

    def __init__(self, context: RuntimeContext) -> None:
        self.context = context
        self._connection: RuntimeConnection | None = None
        self._receive_task: asyncio.Task[None] | None = None
        # A reconnect can page bounded historical turn summaries after the synchronous session
        # attach returns.
        self._history_reconcile_task: asyncio.Task[None] | None = None
        self._live_source: CodexLiveSource | None = None
        self._native_session_id: str | None = None
        self._active_turn_id: str | None = None
        self._thread_status: str | None = None
        self._pending_interaction: NativeHumanInteraction | None = None
        self._settings = RuntimeSettings(
            model=context.model, reasoning_effort=context.reasoning_effort
        )
        self._settings_available: bool | None = None
        self._settings_gate_reason: CapabilityUnavailableReason | None = None
        self._subscribed = False
        self._health = ConnectionHealth.UNOPENED
        self._native_version: str | None = None
        self._diagnostics: deque[str] = deque(maxlen=CODEX_RUNTIME_DIAGNOSTICS_MAX)
        # ---- UI-first NEW discovery ---------------------------------------
        self._started_threads: deque[dict[str, object]] = deque(maxlen=8)
        self._thread_started_event = asyncio.Event()
        # Events/facts may degrade on overflow; terminal evidence uses loss-free bounded
        # backpressure.
        self._events: deque[Event] = deque(maxlen=CODEX_RUNTIME_EVENTS_BUFFER)
        self._facts: deque = deque(maxlen=CODEX_RUNTIME_FACTS_BUFFER)
        self._outcomes: asyncio.Queue[NativeTurnOutcome] = asyncio.Queue(
            maxsize=CODEX_RUNTIME_OUTCOMES_BUFFER
        )
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

    # ---- session ---------------------------------------------------------

    async def open_session(
        self,
        *,
        mode: SessionOpenMode,
        native_session_id: str | None = None,
    ) -> RuntimeBinding:
        if self._native_session_id is not None:
            raise RuntimeError("codex runtime session is already opened")
        if not isinstance(mode, SessionOpenMode):
            raise TypeError("open_session mode must be a SessionOpenMode")
        await self._connect()
        if mode is SessionOpenMode.NEW:
            session_id = await self._open_ui_created_session(native_session_id)
        elif mode is SessionOpenMode.FORK:
            session_id = await self._open_forked_session(native_session_id)
        elif mode is SessionOpenMode.RECONNECT:
            session_id = await self._open_reconnected_session(native_session_id)
        else:
            raise ValueError(f"unsupported session open mode: {mode!r}")
        self._native_session_id = session_id
        await self._probe_settings_gate()
        if mode is SessionOpenMode.RECONNECT:
            self._start_history_reconciliation(session_id)
        return self._binding()

    async def _open_ui_created_session(self, native_session_id: str | None) -> str:
        if native_session_id is not None:
            raise ValueError(
                "open_session(NEW) opens the UI-created session; pass native_session_id=None"
            )
        thread = await self._await_ui_thread()
        session_id = _thread_id_of(thread)
        if session_id is None:
            raise RuntimeConnectionError(
                "thread/started broadcast carried no usable native thread id"
            )
        self._thread_status = _thread_status_type(thread)
        # A zero-turn thread has no rollout yet: thread/resume would fail with "no rollout found".
        return session_id

    async def _open_forked_session(self, native_session_id: str | None) -> str:
        parent = _bounded_str(native_session_id, limit=512)
        if parent is None:
            raise ValueError("open_session(FORK) requires the exact parent native session id")
        result = await self._request("thread/fork", {"threadId": parent})
        forked = result.get("thread") if isinstance(result, Mapping) else None
        forked_thread: Mapping[str, object] | None = forked if isinstance(forked, Mapping) else None
        session_id = _thread_id_of(forked_thread)
        if session_id is None:
            raise RuntimeConnectionError("thread/fork did not return the forked native thread id")
        self._thread_status = _thread_status_type(forked_thread) if forked_thread else None
        # Bind the exact fork before explicit resume subscription; requester subscription is not
        # presumed.
        self._native_session_id = session_id
        await self._subscribe_after_rollout()
        return session_id

    async def _open_reconnected_session(self, native_session_id: str | None) -> str:
        expected = _bounded_str(native_session_id, limit=512)
        if expected is None:
            raise ValueError("open_session(RECONNECT) requires the exact native session id")
        # Ask the verified app-server for only the same tiny current-state page that the synchronous
        # reconciliation consumes.
        result = await self._request("thread/resume", _resume_params(expected))
        await self._reconcile_resume_result(result, expected)
        return expected

    async def _reconcile_resume_result(self, result: Mapping[str, object], expected: str) -> None:
        resumed = result.get("thread") if isinstance(result, Mapping) else None
        resumed_thread: Mapping[str, object] | None = (
            resumed if isinstance(resumed, Mapping) else None
        )
        attached = _thread_id_of(resumed_thread)
        if attached != expected:
            # Identity mismatch fails closed; never attach by cwd resemblance.
            raise RuntimeConnectionError(
                "thread/resume attached a different native thread "
                f"({attached!r} != {expected!r}); refusing to bind"
            )
        page = result.get("initialTurnsPage")
        turns = page.get("data") if isinstance(page, Mapping) else None
        if not isinstance(turns, (list, tuple)) or len(turns) > CODEX_RUNTIME_RECONCILE_TURNS:
            raise RuntimeConnectionError(
                "thread/resume did not return the requested bounded initialTurnsPage; "
                "refusing full-history hydration or an incomplete current-state view"
            )
        if resumed_thread is not None:
            self._subscribed = True
            # The separate page is newest-first; reconciliation consumes an
            # oldest-first bounded view. Never fall back to thread.turns.
            await self._reconcile_thread(resumed_thread, expected, turns=tuple(reversed(turns)))

    async def frontend_plan(self, *, native_session_id: str | None = None) -> LaunchPlan:
        endpoint = self.context.endpoint
        if not endpoint:
            raise ValueError("codex frontend plan requires the private backend endpoint")
        # Initialize the observer before returning the promptless UI plan so eager thread/start
        # cannot be missed.
        await self._connect()
        return plan_codex_frontend(endpoint, native_session_id=native_session_id)

    def live_source(self) -> Source:
        # Share one Source per runtime so status cursors and terminal-buffer ownership cannot
        # compete.
        if self._live_source is None:
            self._live_source = CodexLiveSource(self)
        return self._live_source

    async def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            native_session_id=self._native_session_id,
            native_turn_id=self._active_turn_id,
            pending_interaction=self._pending_interaction,
            settings=self._settings,
            capabilities=self._capabilities(),
            health=self._health,
            health_diagnostics=tuple(self._diagnostics),
            execution_state=self._execution_state(),
        )

    def _execution_state(self) -> RuntimeExecutionState:
        """Plugin-confirmed execution state from native Codex state only."""
        if self._active_turn_id is not None or self._thread_status == "active":
            return RuntimeExecutionState.ACTIVE
        if (
            self._thread_status == "idle"
            and self._native_session_id is not None
            and self._health in (ConnectionHealth.CONNECTED, ConnectionHealth.DEGRADED)
        ):
            return RuntimeExecutionState.IDLE
        return RuntimeExecutionState.UNKNOWN

    # ---- controls --------------------------------------------------------

    async def send(self, *, operation_id: str, prompt: str) -> ControlReceipt:
        session = self._require_session()
        params: dict[str, object] = {
            "threadId": session,
            "input": [{"type": "text", "text": prompt}],
            # Client message id for native correlation only; the operation id
            # itself is the durable Theater fact.
            "clientUserMessageId": operation_id,
        }
        try:
            try:
                result = await self._request("turn/start", params)
            except RuntimeRequestError as error:
                return self._rejected(operation_id, "turn_start_refused", error.message)
            except RuntimeRequestTimeout:
                return self._unknown_prompt_start(operation_id, "control_ack_timeout")
            except (RuntimeConnectionClosed, RuntimeConnectionError) as error:
                return self._unknown_prompt_start(operation_id, "connection_lost", str(error))
            turn = result.get("turn") if isinstance(result, Mapping) else None
            turn_id = _bounded_str(turn.get("id") if isinstance(turn, Mapping) else None, limit=512)
            if turn_id is None:
                # Accepted but uncorrelatable: never fabricate a turn identity.
                return self._unknown_prompt_start(operation_id, "malformed_turn_start_result")
            # The returned turn IS the turn to report: a simultaneous native-UI submission absorbs
            # this message into the already-active turn and the backend returns that same turn id.
            self._active_turn_id = turn_id
            self._thread_status = "active"
            await self._subscribe_after_rollout()
            return ControlReceipt(
                operation_id=operation_id,
                result=DeliveryResult.ACCEPTED,
                native_turn_id=turn_id,
            )
        except asyncio.CancelledError:
            # A cancellation can arrive after ``turn/start`` crossed the transport write — including
            # while waiting for subscription after an acknowledgement.
            self._unknown_prompt_start(operation_id, "control_cancelled")
            raise

    async def steer(
        self,
        *,
        operation_id: str,
        native_turn_id: str,
        prompt: str,
    ) -> ControlReceipt:
        session = self._require_session()
        expected = _bounded_str(native_turn_id, limit=512)
        if expected is None:
            return self._rejected(operation_id, "stale_turn", "expectedTurnId is required")
        params: dict[str, object] = {
            "threadId": session,
            "expectedTurnId": expected,
            "input": [{"type": "text", "text": prompt}],
        }
        try:
            result = await self._request("turn/steer", params)
        except RuntimeRequestError as error:
            # A stale-turn refusal stays a refusal — never reinterpreted as a
            # send or a queued message.
            code = "stale_turn" if "no active turn" in error.message else "steer_refused"
            return self._rejected(operation_id, code, error.message)
        except RuntimeRequestTimeout:
            return self._unknown(operation_id, "control_ack_timeout")
        except (RuntimeConnectionClosed, RuntimeConnectionError) as error:
            return self._unknown(operation_id, "connection_lost", str(error))
        steered = _bounded_str(
            result.get("turnId") if isinstance(result, Mapping) else None, limit=512
        )
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=steered or expected,
        )

    async def interrupt(
        self,
        *,
        operation_id: str,
        native_turn_id: str | None = None,
    ) -> ControlReceipt:
        session = self._require_session()
        turn_id = _bounded_str(native_turn_id, limit=512) or self._active_turn_id
        if turn_id is None:
            return self._rejected(
                operation_id,
                "no_active_turn",
                "no active native turn to interrupt on this thread",
            )
        try:
            await self._request("turn/interrupt", {"threadId": session, "turnId": turn_id})
        except RuntimeRequestError as error:
            return self._rejected(operation_id, "interrupt_refused", error.message)
        except RuntimeRequestTimeout:
            return self._unknown(operation_id, "control_ack_timeout")
        except (RuntimeConnectionClosed, RuntimeConnectionError) as error:
            return self._unknown(operation_id, "connection_lost", str(error))
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.ACCEPTED,
            native_turn_id=turn_id,
        )

    async def update_settings(
        self,
        *,
        operation_id: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> ControlReceipt:
        session = self._require_session()
        if model is None and reasoning_effort is None:
            return self._rejected(
                operation_id, "no_settings_fields", "supply model and/or reasoning_effort"
            )
        if self._thread_status == "active" or self._active_turn_id is not None:
            # Idle-only, rechecked at dispatch: a busy thread never changes settings.
            return self._rejected(operation_id, "session_busy", "settings updates are idle-only")
        if self._settings_available is False:
            return self._rejected(
                operation_id,
                "settings_unavailable",
                f"thread/settings/update is unavailable on this backend: "
                f"{self._settings_gate_reason or CapabilityUnavailableReason.GATED_BY_BACKEND}",
            )
        params: dict[str, object] = {"threadId": session}
        if model is not None:
            params["model"] = model
        if reasoning_effort is not None:
            params["effort"] = reasoning_effort
        try:
            await self._request("thread/settings/update", params)
            self._settings_available = True
            self._settings_gate_reason = None
        except RuntimeRequestError as error:
            self._mark_settings_gate(error)
            return self._rejected(
                operation_id,
                "settings_unavailable",
                f"thread/settings/update refused: {error.message}",
            )
        except RuntimeRequestTimeout:
            return self._unknown(operation_id, "control_ack_timeout")
        except (RuntimeConnectionClosed, RuntimeConnectionError) as error:
            return self._unknown(operation_id, "connection_lost", str(error))
        confirmed = await self._readback_settings(session)
        if not confirmed:
            # Unconfirmed readback leaves settings untouched and delivery UNKNOWN, never optimistic
            # success.
            self._degrade(
                "settings update accepted but unconfirmed by native readback; "
                "confirmed settings unchanged"
            )
            return self._unknown(
                operation_id,
                "settings_unconfirmed",
                "backend accepted thread/settings/update but native readback "
                "could not confirm effective settings",
            )
        return ControlReceipt(operation_id=operation_id, result=DeliveryResult.ACCEPTED)

    async def aclose(self) -> None:
        """Disconnect Theater's connection only; never terminate the backend."""
        receive_task = self._receive_task
        self._receive_task = None
        history_task = self._history_reconcile_task
        self._history_reconcile_task = None
        connection = self._connection
        self._connection = None
        tasks = (
            (receive_task, "receive loop"),
            (history_task, "history reconciliation"),
        )
        for task, _ in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task, label in tasks:
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:  # pragma: no cover - defensive
                self._diagnostic(f"{label} ended: {error}")
        if connection is not None:
            try:
                await connection.aclose()
            except Exception as error:  # pragma: no cover - defensive
                self._diagnostic(f"connection close failed: {error}")
        self._subscribed = False
        self._health = ConnectionHealth.DISCONNECTED

    # ---- connection and handshake -----------------------------------------

    async def _abandon_connection(self, connection: RuntimeConnection) -> None:
        """Discard a connection whose handshake failed; never leak it."""
        self._connection = None
        try:
            await connection.aclose()
        except Exception as error:  # pragma: no cover - defensive
            self._diagnostic(f"handshake-failure close failed: {error}")

    async def _connect(self) -> None:
        if self._connection is not None:
            return
        endpoint = self.context.endpoint
        if not endpoint:
            raise ValueError("codex runtime requires the private backend endpoint")
        connection = await self.context.io.connect(
            endpoint, timeout=CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS
        )
        self._connection = connection
        try:
            result = await connection.request(
                "initialize", _initialize_params, timeout=CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS
            )
        except BaseException:
            await self._abandon_connection(connection)
            raise
        native_version = _version_from_user_agent(
            result.get("userAgent") if isinstance(result, Mapping) else None
        )
        # Recheck the verified version at handshake; the binary may have changed since the
        # subprocess probe.
        if native_version not in CODEX_RUNTIME_VERIFIED_VERSIONS:
            await self._abandon_connection(connection)
            raise RuntimeConnectionError(
                "codex app-server handshake reported unverified native version "
                f"{native_version!r}; compatibility policy "
                f"{CODEX_RUNTIME_COMPATIBILITY_POLICY} verifies only "
                f"{', '.join(sorted(CODEX_RUNTIME_VERIFIED_VERSIONS))}"
            )
        try:
            # The dialect requires the initialized notification exactly once after initialize; no
            # jsonrpc field ever appears (the injected connection owns the wire framing).
            await connection.notify("initialized", {})
        except BaseException:
            await self._abandon_connection(connection)
            raise
        self._native_version = native_version
        self._health = ConnectionHealth.CONNECTED
        self._diagnostics.clear()
        self._receive_task = asyncio.create_task(
            self._receive_loop(), name=f"codex-runtime-{self.context.participant_id}"
        )

    async def _request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        connection = self._require_connection()
        return await connection.request(
            method, params, timeout=CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS
        )

    def _require_connection(self) -> RuntimeConnection:
        if self._connection is None:
            raise RuntimeConnectionClosed("codex runtime connection is not open")
        return self._connection

    def _require_session(self) -> str:
        if self._native_session_id is None:
            raise RuntimeError("codex runtime session is not opened")
        return self._native_session_id

    async def _receive_loop(self) -> None:
        connection = self._connection
        if connection is None:
            return
        try:
            async for notification in connection.notifications():
                await self._handle_notification(notification)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._health = ConnectionHealth.DISCONNECTED
            self._diagnostic(f"native notification stream failed: {error}")
            # The health transition itself is readable state: wake the live source/observer so a
            # waiting reader notices the disconnect.
            self._notify_activity()
        else:
            self._health = ConnectionHealth.DISCONNECTED
            self._diagnostic("native notification stream ended")
            self._notify_activity()

    async def _await_ui_thread(self) -> Mapping[str, object]:
        """Wait for the exact UI-created thread on this private backend."""
        deadline = time.monotonic() + CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS
        while True:
            candidates = [
                thread for thread in self._started_threads if thread.get("ephemeral") is not True
            ]
            cwd = self.context.cwd
            if cwd is not None:
                exact = [thread for thread in candidates if thread.get("cwd") == cwd]
                if len(exact) == 1:
                    return exact[0]
                if len(exact) > 1:
                    raise RuntimeConnectionError(
                        "multiple thread/started broadcasts match the participant cwd; "
                        "refusing to guess a native session"
                    )
            else:
                if len(candidates) == 1:
                    return candidates[0]
                if len(candidates) > 1:
                    raise RuntimeConnectionError(
                        "multiple thread/started broadcasts and no cwd predicate; "
                        "refusing to guess a native session"
                    )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeConnectionError(
                    "no thread/started broadcast from the private backend within the "
                    "startup deadline; launch the promptless native UI after the observer "
                    "connection initializes"
                )
            self._thread_started_event.clear()
            try:
                await asyncio.wait_for(self._thread_started_event.wait(), timeout=remaining)
            except TimeoutError:
                continue

    def _binding(self) -> RuntimeBinding:
        return RuntimeBinding(
            participant_id=self.context.participant_id,
            backend_generation=self.context.backend_generation,
            wiring=RuntimeWiring.NATIVE,
            lifecycle=RuntimeLifecyclePhase.BOUND,
            endpoint=self.context.endpoint,
            pid=None,
            native_session_id=self._native_session_id,
            protocol="codex-app-server",
            protocol_version=self._native_version,
            native_version=self._native_version,
            compatibility_policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
        )

    # ---- subscription and gap recovery -------------------------------------

    async def _subscribe_after_rollout(self) -> None:
        """Subscribe once the returned turn materializes the rollout."""
        if self._subscribed or self._connection is None:
            return
        session = self._native_session_id
        if session is None:
            return
        try:
            result = await self._connection.request(
                "thread/resume",
                _resume_params(session),
                timeout=CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS,
            )
        except RuntimeRequestError as error:
            if "no rollout found" in error.message:
                return
            self._degrade(f"thread/resume subscription failed: {error.message}")
            return
        except (RuntimeRequestTimeout, RuntimeConnectionClosed, RuntimeConnectionError) as error:
            self._degrade(f"thread/resume subscription failed: {error}")
            return
        await self._reconcile_resume_result(result, session)

    async def _reconcile_thread(
        self,
        thread: Mapping[str, object],
        session: str,
        *,
        turns: Sequence[object],
    ) -> None:
        """Reconcile reconnect/subscription gaps from a native thread payload."""
        self._thread_status = _thread_status_type(thread)
        active = None
        if isinstance(turns, (list, tuple)):
            # Slice before filtering so an unexpectedly long response cannot create an unbounded
            # pre-observer copy.
            recent = [
                turn for turn in turns[-CODEX_RUNTIME_RECONCILE_TURNS:] if isinstance(turn, Mapping)
            ]
            for turn in reversed(recent):
                turn_id = _bounded_str(turn.get("id"), limit=512)
                status = turn.get("status")
                if turn_id is None or not isinstance(status, str):
                    continue
                if status == "inProgress":
                    if active is None:
                        active = turn_id
                    continue
                await self._record_snapshot_terminal_turn(session, turn)
        if active is not None:
            self._active_turn_id = active
        self._adopt_thread_settings(thread)

    def _start_history_reconciliation(self, session: str) -> None:
        """Own one cooperative exact-history task across bounded passes."""
        task = self._history_reconcile_task
        if task is not None and not task.done():
            return
        self._history_reconcile_task = asyncio.create_task(
            self._reconcile_history(session),
            name=f"codex-runtime-history-{self.context.participant_id}",
        )

    async def _reconcile_history(self, session: str) -> None:
        """Page older exact terminals without retaining unbounded history."""
        cursor: str | None = None
        # Brent's cursor-cycle detector needs constant memory even when a healthy session has
        # arbitrarily many pages.
        anchor: str | None = None
        power, distance = 1, 0
        pages = 0
        retry_delay = CODEX_RUNTIME_RECONCILE_RETRY_SECONDS
        while self._native_session_id == session and self._connection is not None:
            connection = self._connection
            try:
                params: dict[str, object] = {
                    "threadId": session,
                    "limit": CODEX_RUNTIME_RECONCILE_PAGE_SIZE,
                    "itemsView": "summary",
                    "sortDirection": "desc",
                }
                if cursor is not None:
                    params["cursor"] = cursor
                result = await self._request("thread/turns/list", params)
                if self._native_session_id != session or self._connection is not connection:
                    return
                for turn in _history_turns(result):
                    if isinstance(turn, Mapping):
                        await self._record_snapshot_terminal_turn(session, turn)
                next_cursor = result.get("nextCursor") if isinstance(result, Mapping) else None
                if next_cursor is None:
                    return
                next_page = _bounded_str(next_cursor, limit=4096)
                if next_page in (None, cursor, anchor):
                    # A stale/invalid cursor cannot advance safely.
                    cursor = anchor = None
                    power, distance = 1, 0
                    self._diagnostic(
                        "thread/turns/list returned an invalid or repeated cursor; retrying"
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, CODEX_RUNTIME_RECONCILE_MAX_RETRY_SECONDS)
                    continue
                cursor = next_page
                distance += 1
                if distance == power:
                    anchor = cursor
                    power *= 2
                    distance = 0
                retry_delay = CODEX_RUNTIME_RECONCILE_RETRY_SECONDS
                pages += 1
                # Yield between pages; terminal-queue backpressure independently bounds retained
                # evidence.
                if pages >= CODEX_RUNTIME_RECONCILE_MAX_PAGES:
                    self._diagnostic(
                        "thread/turns/list yielded a bounded recovery pass; continuing"
                    )
                    pages = 0
                    await asyncio.sleep(CODEX_RUNTIME_RECONCILE_PAUSE_SECONDS)
                else:
                    await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._diagnostic(f"thread/turns/list recovery will retry: {error}")
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, CODEX_RUNTIME_RECONCILE_MAX_RETRY_SECONDS)

    async def _record_snapshot_terminal_turn(
        self, session: str, turn: Mapping[str, object]
    ) -> None:
        """Emit one exact terminal from a read-only history/snapshot view."""
        turn_id = _bounded_str(turn.get("id"), limit=512)
        status = turn.get("status")
        if turn_id is None or not isinstance(status, str):
            return
        terminal = _TERMINAL_BY_STATUS.get(status)
        if terminal is None:
            return
        # History is not a live terminal notification; snapshot-derived results remain PARTIAL.
        await self._record_turn_outcome(
            session,
            turn_id,
            terminal,
            result=_agent_message_text(turn.get("items")),
            completeness=ResultCompleteness.PARTIAL,
            provenance=ResultProvenance.NATIVE_EVIDENCE,
            error=_turn_error_message(turn.get("error")),
            from_history=True,
            completed_at=_completed_at(turn),
        )

    # ---- settings ----------------------------------------------------------

    async def _probe_settings_gate(self) -> None:
        """Honestly determine the experimental settings gate, idle-only."""
        if self._thread_status == "active" or self._active_turn_id is not None:
            return
        session = self._native_session_id
        if session is None or self._connection is None:
            return
        try:
            await self._connection.request(
                "thread/settings/update",
                {"threadId": session},
                timeout=CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS,
            )
        except RuntimeRequestError as error:
            self._mark_settings_gate(error)
            return
        except (RuntimeRequestTimeout, RuntimeConnectionClosed, RuntimeConnectionError) as error:
            self._diagnostic(f"settings gate undetermined: {error}")
            return
        self._settings_available = True
        self._settings_gate_reason = None

    def _mark_settings_gate(self, error: RuntimeRequestError) -> None:
        self._settings_available = False
        self._settings_gate_reason = (
            CapabilityUnavailableReason.GATED_BY_BACKEND
            if error.code == -32600 or "experimentalApi" in error.message
            else CapabilityUnavailableReason.NOT_DETERMINED
        )

    async def _readback_settings(self, session: str) -> bool:
        """Confirm effective settings from native readback, never emulation."""
        try:
            result = await self._request(
                "thread/read", {"threadId": session, "includeTurns": False}
            )
        except (RuntimeRequestError, RuntimeRequestTimeout, RuntimeConnectionError):
            self._diagnostic("settings readback unavailable; application stays uncertain")
            return False
        thread = result.get("thread") if isinstance(result, Mapping) else result
        if not isinstance(thread, Mapping):
            self._diagnostic("settings readback carried no thread; application stays uncertain")
            return False
        has_model = isinstance(thread.get("model"), str)
        has_effort = isinstance(thread.get("reasoningEffort"), str) or isinstance(
            thread.get("effort"), str
        )
        if not (has_model or has_effort):
            self._diagnostic(
                "settings readback carried no settings fields; application stays uncertain"
            )
            return False
        self._adopt_thread_settings(thread)
        return True

    def _adopt_thread_settings(self, thread: Mapping[str, object]) -> bool:
        model = _bounded_str(thread.get("model"), limit=512)
        effort = _bounded_str(thread.get("reasoningEffort") or thread.get("effort"), limit=512)
        if model is None and effort is None:
            return False
        self._settings = RuntimeSettings(model=model, reasoning_effort=effort)
        return True

    # ---- notification normalization ----------------------------------------

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
            questions = item.get("questions")
            if isinstance(questions, (list, tuple)) and questions:
                self._pending_interaction = NativeHumanInteraction(
                    kind=NativeInteractionKind.CLARIFICATION,
                    native_item_id=item_id,
                    native_turn_id=turn_id,
                    details=_clarification_details(questions),
                )
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
        if method.endswith(_APPROVAL_METHOD_SUFFIX):
            kind = NativeInteractionKind.APPROVAL
        elif any(marker in method for marker in _CLARIFICATION_METHOD_MARKERS):
            kind = NativeInteractionKind.CLARIFICATION
        else:
            self._diagnostic(f"observed unclassified server request {method}")
            return
        details = params.get("reason")
        if not isinstance(details, str) or not details:
            details = method
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
        self._notify_activity()

    # ---- shared normalization helpers --------------------------------------

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

    def _capabilities(self) -> RuntimeCapabilities:
        available: set[RuntimeCapability] = set()
        unavailable: dict[RuntimeCapability, CapabilityUnavailableReason] = {
            RuntimeCapability.QUEUE_FOLLOWUP: CapabilityUnavailableReason.THEATER_POLICY,
        }
        bound = self._native_session_id is not None
        if bound:
            available.add(RuntimeCapability.SEND)
            available.add(RuntimeCapability.INTERRUPT)
        else:
            unavailable[RuntimeCapability.SEND] = CapabilityUnavailableReason.SESSION_STATE
            unavailable[RuntimeCapability.INTERRUPT] = CapabilityUnavailableReason.SESSION_STATE
        if self._active_turn_id is not None:
            available.add(RuntimeCapability.STEER)
        else:
            unavailable[RuntimeCapability.STEER] = CapabilityUnavailableReason.SESSION_STATE
        if self._settings_available is True:
            available.add(RuntimeCapability.SETTINGS_UPDATE)
        else:
            unavailable[RuntimeCapability.SETTINGS_UPDATE] = (
                self._settings_gate_reason
                if self._settings_gate_reason is not None
                else CapabilityUnavailableReason.NOT_DETERMINED
            )
        return RuntimeCapabilities(available=frozenset(available), unavailable_reasons=unavailable)

    def _rejected(self, operation_id: str, code: str, message: str) -> ControlReceipt:
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.REJECTED,
            error_code=code,
            error=clip(message) or None,
        )

    def _unknown(self, operation_id: str, code: str, detail: str | None = None) -> ControlReceipt:
        # UNKNOWN delivery: no retry, no tmux fallback; reconciliation only.
        message = f"{code}: native transmission or acceptance was uncertain"
        if detail:
            message = f"{message} ({detail})"
        self._diagnostic(message)
        return ControlReceipt(
            operation_id=operation_id,
            result=DeliveryResult.UNKNOWN,
            error_code=code,
            error=clip(message) or None,
        )

    def _unknown_prompt_start(
        self, operation_id: str, code: str, detail: str | None = None
    ) -> ControlReceipt:
        """Fail closed before returning an ambiguous prompt-start receipt."""
        self._active_turn_id = None
        self._thread_status = None
        self._status_hint = None
        self._subscribed = False
        self._health = ConnectionHealth.DISCONNECTED
        self._diagnostic(f"{code}: ambiguous turn/start invalidated cached native session state")
        # The observer's existing activity hook wakes both its status reader and the daemon
        # composition's recovery path.
        self._notify_activity()
        self._health = ConnectionHealth.DISCONNECTED
        return self._unknown(operation_id, code, detail)


_NOTIFICATION_HANDLERS: dict = {
    "thread/started": CodexRuntime._on_thread_started,
    "thread/status/changed": CodexRuntime._on_thread_status_changed,
    "turn/started": CodexRuntime._on_turn_started,
    "turn/completed": CodexRuntime._on_turn_completed,
    # Only item completions enter the ledger; marking item/started would suppress later completion.
    "item/agentMessage/delta": CodexRuntime._on_agent_message_delta,
    "item/completed": CodexRuntime._on_item_completed,
    "thread/settings/updated": CodexRuntime._on_settings_updated,
    "serverRequest/resolved": CodexRuntime._on_server_request_resolved,
}


class CodexLiveSource(Source):
    """The single live ``Source`` of one Codex runtime."""

    def __init__(self, runtime: CodexRuntime) -> None:
        self._runtime = runtime
        self._last_status: Status | None = None

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        """Forward the optional arrival-driven wake hook to the runtime."""
        self._runtime.set_activity_callback(callback)

    async def read(self) -> Batch:
        runtime = self._runtime
        events: list[Event] = []
        while runtime._events and len(events) < CODEX_RUNTIME_EVENTS_PER_BATCH:
            events.append(runtime._events.popleft())
        facts: list = []
        previews = self._drain_delta_previews()
        while runtime._facts and len(facts) + len(previews) < CODEX_RUNTIME_EVENTS_PER_BATCH:
            facts.append(runtime._facts.popleft())
        facts.extend(previews)
        evidence = []
        while True:
            # Each terminal removal releases one backpressured insertion; never discard exact
            # outcomes.
            try:
                evidence.append(runtime._outcomes.get_nowait())
            except asyncio.QueueEmpty:
                break
        status = self._status()
        status_changed = status != self._last_status
        self._last_status = status
        progressed = bool(events or facts or evidence or status_changed)
        has_more = bool(runtime._events or runtime._facts or not runtime._outcomes.empty())
        return Batch(
            events=events,
            progressed=progressed,
            has_more=has_more,
            status=status,
            trajectory=facts,
            terminal_evidence=evidence,
        )

    def _drain_delta_previews(self) -> list:
        runtime = self._runtime
        previews: list = []
        for item_id, text in list(runtime._delta_items.items()):
            seen = runtime._delta_previewed_chars.get(item_id, 0)
            if len(text) <= seen:
                continue
            runtime._delta_previewed_chars[item_id] = len(text)
            previews.append(
                _fact(
                    kind=TrajectoryKind.ASSISTANT,
                    summary=ContentPreview.from_text(text).text,
                    native_id=item_id,
                    turn_id=runtime._active_turn_id,
                    status=TrajectoryStatus.RUNNING,
                    lane=TrajectoryLane.MODEL,
                )
            )
        return previews

    def _status(self) -> Status | None:
        runtime = self._runtime
        if runtime._pending_interaction is not None:
            # Display hint only; never a control decision input.
            return Status.AWAITING_INPUT
        return runtime._status_hint

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        runtime = self._runtime
        if runtime._health is ConnectionHealth.DEGRADED:
            state = ChannelHealthState.DEGRADED
        elif runtime._health is ConnectionHealth.DISCONNECTED:
            state = ChannelHealthState.FAILED
        elif runtime._health is ConnectionHealth.CONNECTED:
            state = ChannelHealthState.HEALTHY
        else:
            state = ChannelHealthState.STARTING
        return (
            ChannelHealth(
                channel_id=_LIVE_CHANNEL_ID,
                state=state,
                diagnostics=tuple(runtime._diagnostics),
                dropped=runtime._dropped,
                accepted=runtime._accepted,
            ),
        )


def _fact(
    *,
    kind: TrajectoryKind,
    summary: str,
    native_id: str | None,
    turn_id: str | None,
    status: TrajectoryStatus,
    lane: TrajectoryLane | None = None,
) -> object:
    from theater.harness.contracts.trajectory import TrajectoryFact

    return TrajectoryFact(
        kind=kind,
        summary=summary,
        source="codex-live",
        lane=lane,
        status=status,
        native_id=native_id,
        turn_id=turn_id,
    )


def _native_revision(item: Mapping[str, object]) -> int:
    """The bounded, non-negative native revision of one completed item."""
    revision = item.get("revision")
    if type(revision) is not int or revision < 0:
        return 0
    return min(revision, CODEX_RUNTIME_REVISION_MAX)


def _user_message_text(content: object) -> str:
    if not isinstance(content, (list, tuple)):
        return ""
    parts = [
        part.get("text")
        for part in content
        if isinstance(part, Mapping) and part.get("type") == "text"
    ]
    return "\n".join(text for text in parts if isinstance(text, str))


def _agent_message_text(items: object) -> str | None:
    if not isinstance(items, (list, tuple)):
        return None
    parts: list[str] = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts) if parts else None


def _summary_view_carries_exact_final_message(
    terminal: NativeTurnTerminal, items_view: object
) -> bool:
    """Whether a summary item view is guaranteed to be the exact final result."""
    return terminal is NativeTurnTerminal.COMPLETED and items_view == "summary"


def _item_summary(item: Mapping[str, object]) -> str | None:
    item_type = item.get("type")
    if not isinstance(item_type, str) or not item_type:
        return None
    label = f"codex item: {item_type}"
    command = item.get("command")
    if isinstance(command, str) and command:
        return f"{label} {command[:160]}"
    return label


def _clarification_details(questions: Sequence) -> str:
    titles: list[str] = []
    for question in questions:
        if isinstance(question, Mapping) and isinstance(question.get("title"), str):
            titles.append(question["title"])
    return " | ".join(titles)[:240] if titles else "clarification questions"


def _turn_error_message(error: object) -> str | None:
    if not isinstance(error, Mapping):
        return None
    message = error.get("message")
    return message if isinstance(message, str) and message else None


def _thread_status_type(thread: Mapping[str, object]) -> str | None:
    status = thread.get("status")
    if isinstance(status, Mapping) and isinstance(status.get("type"), str):
        return status["type"]
    return None


def _seconds_from_ms(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / 1000.0


def _version_from_user_agent(user_agent: object) -> str | None:
    if not isinstance(user_agent, str):
        return None
    head = user_agent.split(" ", 1)[0]
    if "/" not in head:
        return None
    version = head.rsplit("/", 1)[-1]
    return version if version and version[0].isdigit() else None


def codex_runtime_factory(context: RuntimeContext) -> HarnessRuntime:
    """The manifest factory: one CodexRuntime per participant."""
    return CodexRuntime(context)


__all__ = [
    "CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS",
    "CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS",
    "CodexLiveSource",
    "CodexRuntime",
    "codex_runtime_factory",
]
