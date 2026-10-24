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
    CODEX_RUNTIME_RECONCILE_PAUSE_SECONDS,
    CODEX_RUNTIME_RECONCILE_TURNS,
    CODEX_RUNTIME_UNFINISHED_READS,
)
from .runtime_interactions import _waiting_flags_of
from .runtime_messages import (
    _agent_message_text,
    _bounded_str,
    _completed_at,
    _history_page_params,
    _history_turns,
    _resume_params,
    _thread_status_type,
    _turn_error_message,
    _unfinished,
)


class CodexRuntimeHistory(CodexRuntimeHost):
    _history_reconcile_task: asyncio.Task[None] | None
    _idle_recovery_task: asyncio.Task[None] | None
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

    def _schedule_idle_recovery(self) -> None:
        """Recover what the live stream missed once the thread is idle, off the event handler.

        Idle follows turn completion, so the rollout exists and history carries the turn's end.
        """
        if self._native_session_id is None or self._connection is None:
            return
        if self._subscribed and not self._unfinished_turns:
            return
        if self._idle_recovery_task is not None:
            # A second idle broadcast must not stack a concurrent attempt.
            return
        self._idle_recovery_task = asyncio.create_task(
            self._idle_recovery(),
            name=f"codex-runtime-idle-recovery-{self.context.participant_id}",
        )

    async def _idle_recovery(self) -> None:
        try:
            await self._subscribe_after_rollout()
            await self._recover_unfinished_turns()
        finally:
            self._idle_recovery_task = None

    def _defer_unfinished_turn(self, session: str, turn: Mapping[str, object]) -> None:
        """Leave a turn history cannot call over to its live completion; re-read it when idle."""
        turn_id = _bounded_str(turn.get("id"), limit=512)
        if turn_id is None:
            return
        self._unfinished_turns.add(turn_id)
        if self._thread_status != "active":
            # Idle already went by (it precedes turn/completed), so no later idle will re-read it.
            self._schedule_idle_recovery()

    async def _recover_unfinished_turns(self) -> None:
        """Re-read the newest history page; only an end the app-server now records is believed."""
        session = self._native_session_id
        retry_delay = runtime_constants.CODEX_RUNTIME_RECONCILE_RETRY_SECONDS
        for _ in range(CODEX_RUNTIME_UNFINISHED_READS):
            if session is None or not self._unfinished_turns:
                return
            try:
                result = await self._request(
                    "thread/turns/list", _history_page_params(session, None)
                )
                for turn in _history_turns(result):
                    if not isinstance(turn, Mapping):
                        continue
                    turn_id = _bounded_str(turn.get("id"), limit=512)
                    if turn_id in self._unfinished_turns and not _unfinished(turn):
                        self._unfinished_turns.discard(turn_id)
                        await self._record_snapshot_terminal_turn(session, turn)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._diagnostic(f"unfinished-turn history read will retry: {error}")
            if not self._unfinished_turns:
                return
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, CODEX_RUNTIME_RECONCILE_MAX_RETRY_SECONDS)
        # What history never ended is left to the live completion.
        self._unfinished_turns.clear()

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
                    # An active thread cannot have its newest turn already over: the response says
                    # so itself, whatever this runtime has seen since.
                    self._defer_unfinished_turn(session, turn)
                    continue
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
                running, active_id = self._thread_status == "active", self._active_turn_id
                revision = self._status_revision
                result = await self._request(
                    "thread/turns/list", _history_page_params(session, cursor)
                )
                if self._native_session_id != session or self._connection is not connection:
                    return
                turns = _history_turns(result)
                newest = turns[0] if turns and isinstance(turns[0], Mapping) else {}
                # The newest turn may have been the running one: the thread was active with no other
                # turn known to run, or its status moved while the page was being read.
                unsettled = revision != self._status_revision or (
                    running and active_id in (None, newest.get("id"))
                )
                await self._record_history_page(
                    session, turns, defer_newest=cursor is None and unsettled
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
        self, session: str, turns: Sequence[object], *, defer_newest: bool
    ) -> None:
        """Record a page's exact terminals; its newest turn may still have been running."""
        for index, turn in enumerate(turns):
            if not isinstance(turn, Mapping):
                continue
            if defer_newest and index == 0 and _unfinished(turn):
                self._defer_unfinished_turn(session, turn)
            else:
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
