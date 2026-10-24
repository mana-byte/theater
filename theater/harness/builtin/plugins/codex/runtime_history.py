"""Codex native subscription and history reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from theater.harness.contracts.runtime import (
    ResultCompleteness,
    ResultProvenance,
    RuntimeConnectionClosed,
    RuntimeConnectionError,
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
    _unfinished_turns: set[str]
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
            self._degrade(f"thread/resume subscription failed: {error.message}")
            return
        except (RuntimeRequestTimeout, RuntimeConnectionClosed, RuntimeConnectionError) as error:
            self._degrade(f"thread/resume subscription failed: {error}")
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

    def _defer_unfinished_turn(self, turn: Mapping[str, object]) -> None:
        """Hold back a turn that may have been running until history shows its end."""
        turn_id = _bounded_str(turn.get("id"), limit=512)
        if turn_id is None:
            return
        self._unfinished_turns.add(turn_id)
        if self._thread_status != "active":
            # Idle already went by (it precedes turn/completed), so no later idle will page again.
            self._reread_unfinished_turns()

    def _reread_unfinished_turns(self) -> None:
        """Page history for held-back turns; a pass that ends none leaves them for the next idle."""
        if self._unfinished_turns and self._native_session_id is not None:
            self._start_history_reconciliation(self._native_session_id)

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
            for turn in reversed(recent):
                turn_id = _bounded_str(turn.get("id"), limit=512)
                status = turn.get("status")
                if turn_id is None or not isinstance(status, str):
                    continue
                if status == "inProgress":
                    if active is None:
                        active = turn_id
                    continue
                if (
                    turn is recent[-1]
                    and _thread_status_type(thread) == "active"
                    and _unfinished(turn)
                ):
                    # An active thread cannot have its newest turn already over, whatever this
                    # runtime has seen since: the response contradicts itself.
                    self._defer_unfinished_turn(turn)
                await self._record_snapshot_terminal_turn(session, turn)
        if active is not None and (
            status_revision is None or status_revision == self._status_revision
        ):
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
        retry_delay = runtime_constants.CODEX_RUNTIME_RECONCILE_RETRY_SECONDS
        while self._native_session_id == session and self._connection is not None:
            connection = self._connection
            try:
                # A page has no thread status of its own: take what the runtime knew when asking.
                asked = (
                    self._thread_status == "active",
                    self._active_turn_id,
                    self._status_revision,
                )
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
                await self._record_history_page(
                    session, _history_turns(result), asked if cursor is None else None
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

    async def _record_history_page(
        self,
        session: str,
        turns: Sequence[object],
        asked: tuple[bool, str | None, int] | None,
    ) -> None:
        """Record a page's exact terminals; ``asked`` is the state a first page was requested in."""
        if asked is not None:
            running, active_id, revision = asked
            newest = turns[0] if turns and isinstance(turns[0], Mapping) else {}
            # The newest turn may have been the running one: the thread was active with no other
            # turn known to run, or its status moved while the page was being read.
            if _unfinished(newest) and (
                revision != self._status_revision
                or (running and active_id in (None, newest.get("id")))
            ):
                self._defer_unfinished_turn(newest)
        for turn in turns:
            if isinstance(turn, Mapping):
                await self._record_snapshot_terminal_turn(session, turn)

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
        if turn_id in self._unfinished_turns:
            if _unfinished(turn):
                return
            self._unfinished_turns.discard(turn_id)
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
