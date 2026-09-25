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
from .runtime_messages import (
    _agent_message_text,
    _bounded_str,
    _completed_at,
    _history_turns,
    _resume_params,
    _thread_status_type,
    _turn_error_message,
)


class CodexRuntimeHistory(CodexRuntimeHost):
    _history_reconcile_task: asyncio.Task[None] | None
    _subscription_recovery_task: asyncio.Task[None] | None
    _native_session_id: str | None
    _active_turn_id: str | None
    _thread_status: str | None

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
                # The rollout may not exist yet on the very first turn; this
                # is a deferred subscription, never a silent one.
                self._diagnostic(f"thread/resume deferred: no rollout yet for session {session}")
                return
            self._degrade(f"thread/resume subscription failed: {error.message}")
            return
        except (RuntimeRequestTimeout, RuntimeConnectionClosed, RuntimeConnectionError) as error:
            self._degrade(f"thread/resume subscription failed: {error}")
            return
        await self._reconcile_resume_result(result, session)

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
