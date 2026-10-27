"""Codex native subscription and history reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from theater.harness.contracts.runtime import (
    ResultCompleteness,
    ResultProvenance,
    RuntimeConnectionClosed,
    RuntimeConnectionError,
    RuntimeExecutionState,
    RuntimeRequestError,
    RuntimeRequestTimeout,
)
from theater.models import Status

from . import runtime_constants
from ._runtime_host import CodexRuntimeHost
from .runtime_constants import (
    _TERMINAL_BY_STATUS,
    CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS,
    CODEX_RUNTIME_RECONCILE_MAX_PAGES,
    CODEX_RUNTIME_RECONCILE_MAX_RETRY_SECONDS,
    CODEX_RUNTIME_RECONCILE_PAGE_SIZE,
    CODEX_RUNTIME_RECONCILE_PAUSE_SECONDS,
    CODEX_RUNTIME_RECONCILE_TURNS,
)
from .runtime_interactions import _waiting_flags_of
from .runtime_messages import (
    _agent_message_text,
    _bounded_str,
    _completed_at,
    _history_turns,
    _resume_params,
    _thread_status_type,
    _turn_error_message,
    _unfinished,
)


class CodexRuntimeHistory(CodexRuntimeHost):
    _history_reconcile_task: asyncio.Task[None] | None
    _subscription_recovery_task: asyncio.Task[None] | None
    _in_flight_turns: set[str]
    _native_session_id: str | None
    _active_turn_id: str | None
    _thread_status: str | None
    _status_hint: Status | None

    async def _subscribe_after_rollout(self) -> None:
        """Subscribe once the returned turn materializes the rollout."""
        if self._subscribed or self._connection is None:
            return
        session = self._native_session_id
        if session is None:
            return
        status_revision = self._status_revision
        try:
            result = await self._connection.request(
                "thread/resume",
                _resume_params(session),
                timeout=CODEX_RUNTIME_CONTROL_TIMEOUT_SECONDS,
            )
        except RuntimeRequestError as error:
            if "no rollout found" in error.message:
                # The rollout may not exist yet on the very first turn; this
                # is a deferred subscription, never a silent one.
                self._diagnostic(f"thread/resume deferred: no rollout yet for session {session}")
                return
            self._degrade(f"thread/resume subscription failed: {error.message}", subscription=True)
            return
        except (RuntimeRequestTimeout, RuntimeConnectionClosed, RuntimeConnectionError) as error:
            self._degrade(f"thread/resume subscription failed: {error}", subscription=True)
            return
        await self._reconcile_resume_result(result, session, status_revision=status_revision)

    def _schedule_subscription_recovery(self) -> None:
        """Recover a missed rollout subscription from the idle broadcast.

        Idle follows turn completion, so the rollout exists; one attempt per idle, off the handler.
        """
        if self._subscribed or self._native_session_id is None or self._connection is None:
            return
        if self._subscription_recovery_task is not None:
            # A second idle broadcast must not stack a concurrent attempt.
            return
        self._subscription_recovery_task = asyncio.create_task(
            self._subscription_recovery(),
            name=f"codex-runtime-subscribe-{self.context.participant_id}",
        )

    async def _subscription_recovery(self) -> None:
        try:
            await self._subscribe_after_rollout()
        finally:
            self._subscription_recovery_task = None

    def _register_in_flight(self, session: str, turn_id: str) -> None:
        """Hold a running turn until positive evidence of its end; never one already over."""
        if self._outcome_buffer.has_outcome(session, turn_id):
            return
        self._in_flight_turns.add(turn_id)

    def _recover_in_flight_turns(self) -> None:
        """Let history decide turns still in flight on an idle thread, if their live end is lost."""
        if self._in_flight_turns and self._native_session_id is not None:
            self._start_history_reconciliation(self._native_session_id, recovery=True)

    def _history_owed(self) -> bool:
        """Idle with turns in flight: their live completion is due, or was lost."""
        return bool(self._in_flight_turns) and self._execution_state() is RuntimeExecutionState.IDLE

    async def _reconcile_thread(
        self,
        thread: Mapping[str, object],
        session: str,
        *,
        turns: Sequence[object],
        status_revision: int | None = None,
    ) -> None:
        """Reconcile reconnect/subscription gaps from a native thread payload."""
        # A delayed snapshot cannot overwrite live evidence received during its request.
        if status_revision is None or status_revision == self._status_revision:
            self._thread_status = _thread_status_type(thread)
            if self._thread_status == "idle" and _waiting_flags_of(thread.get("status")) is None:
                self._thread_status = None
            if self._thread_status in {"active", "idle"}:
                self._status_hint = (
                    Status.WORKING if self._thread_status == "active" else Status.IDLE
                )
            if self._adopt_waiting_flags(thread.get("status")):
                self._notify_activity()
        active = None
        if isinstance(turns, (list, tuple)):
            # Slice before filtering so an unexpectedly long response cannot create an unbounded
            # pre-observer copy.
            recent = [
                turn for turn in turns[-CODEX_RUNTIME_RECONCILE_TURNS:] if isinstance(turn, Mapping)
            ]
            active = await self._record_snapshot(
                session, recent, newest=True, running=_thread_status_type(thread) == "active"
            )
        if active is not None and (
            status_revision is None or status_revision == self._status_revision
        ):
            self._active_turn_id = active
        self._adopt_thread_settings(thread)

    def _start_history_reconciliation(self, session: str, *, recovery: bool = False) -> None:
        """Own one cooperative exact-history task across bounded passes."""
        task = self._history_reconcile_task
        if task is not None and not task.done():
            return
        self._history_reconcile_task = asyncio.create_task(
            self._reconcile_history(session, recovery=recovery),
            name=f"codex-runtime-history-{self.context.participant_id}",
        )

    async def _reconcile_history(self, session: str, *, recovery: bool = False) -> None:
        """Page exact history; while turns stay in flight on an idle thread, page it again.

        A request made during a pass needs no signal: the pass ends by looking at what is owed.
        """
        if recovery:
            await asyncio.sleep(runtime_constants.CODEX_RUNTIME_RECONCILE_IDLE_GRACE_SECONDS)
            if not self._history_owed():
                return
        delay = runtime_constants.CODEX_RUNTIME_RECONCILE_RETRY_SECONDS
        while self._native_session_id == session and self._connection is not None:
            await self._page_history(session)
            if not self._history_owed():
                return
            # The live completion may still land while waiting.
            await asyncio.sleep(delay)
            delay = min(delay * 2, CODEX_RUNTIME_RECONCILE_MAX_RETRY_SECONDS)
            if not self._history_owed():
                return

    async def _page_history(self, session: str) -> None:
        """One pass over history, paging older exact terminals without unbounded retention."""
        cursor: str | None = None
        # Brent's cursor-cycle detector needs constant memory even when a healthy session has
        # arbitrarily many pages.
        anchor: str | None = None
        power, distance = 1, 0
        pages = 0
        retry_delay = runtime_constants.CODEX_RUNTIME_RECONCILE_RETRY_SECONDS
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
                turns = [t for t in reversed(_history_turns(result)) if isinstance(t, Mapping)]
                # A page has no thread status of its own: the first page ends with the thread's
                # newest turn, which may be running unless the runtime knows the thread idle.
                await self._record_snapshot(
                    session,
                    turns,
                    newest=cursor is None,
                    running=self._execution_state() is not RuntimeExecutionState.IDLE,
                )
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
                retry_delay = runtime_constants.CODEX_RUNTIME_RECONCILE_RETRY_SECONDS
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

    async def _record_snapshot(
        self,
        session: str,
        turns: Sequence[Mapping[str, object]],
        *,
        newest: bool,
        running: bool,
    ) -> str | None:
        """Record a snapshot's exact ends, oldest first; return the running turn it shows.

        ``newest``: the last turn is the thread's newest, the only one that can be running, and
        read as unfinished while the thread may be running it, it is held rather than recorded.
        Oldest first lets a real end resolve its turn before the newest is judged.
        """
        # A held turn this snapshot lacks started after it was taken: its newest turn is older.
        shown = {_bounded_str(turn.get("id"), limit=512) for turn in turns}
        newer_held = any(held not in shown for held in self._in_flight_turns)
        active = None
        for turn in turns:
            turn_id = _bounded_str(turn.get("id"), limit=512)
            status = turn.get("status")
            if turn_id is None or not isinstance(status, str):
                continue
            current = newest and turn is turns[-1]
            if status == "inProgress":
                if current:
                    active = turn_id
                    self._register_in_flight(session, turn_id)
                continue
            if current and running and _unfinished(turn) and not newer_held:
                self._register_in_flight(session, turn_id)
            await self._record_snapshot_terminal_turn(session, turn)
        return active

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
        if _unfinished(turn):
            return  # An interrupted placeholder proves no end, including after a daemon restart.
        if turn_id in self._in_flight_turns:
            self._in_flight_turns.discard(turn_id)
        # Authoritative history is lifecycle evidence too: a terminal turn's own
        # recorded interactions can no longer be answered; other turns survive.
        self._drop_turn_interactions(turn_id)
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
