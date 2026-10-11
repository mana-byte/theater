"""Heuristic job completion, suppressed wherever a live channel owns exact completion."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from sqlalchemy import Connection

from theater.constants.observation import RAW_RESULT_UNSET
from theater.daemon.observation.completion import CompletionTracker
from theater.daemon.observation.live import LiveObservationHub, LiveRegistration
from theater.daemon.observation.reducer import QuietClock
from theater.daemon.observation.turns import TurnAccumulator
from theater.harness import HarnessObserver, TurnTerminal
from theater.harness.contracts.source import NativeTurnState
from theater.models import JobState, now

if TYPE_CHECKING:
    from theater.daemon.store import Store

logger = logging.getLogger("theater.observer")


class CompletionGate:
    if TYPE_CHECKING:
        _completion: CompletionTracker
        live: LiveObservationHub
        rescue: float
        store: Store
        _capture: Callable[..., Any]
        _monotonic: Callable[[], float]

    def _finish(
        self,
        handle: str,
        result_text: str,
        *,
        error_code: str | None = None,
        state: JobState = JobState.DONE,
        raw_result: str | object | None = RAW_RESULT_UNSET,
        registration: LiveRegistration | None = None,
    ) -> None:
        job = self.store.get_job(handle)
        pid = job.target_id if job is not None and job.target_id else handle.partition("#")[0]
        if self._live_completion_owned(pid, registration):
            # Live-wired jobs finish only through exact terminal evidence; these heuristics could
            # invent completion. Stays in force while an old watcher is cancelled after replace.
            logger.debug(
                "live-wired %s: heuristic finish of %s suppressed for exact evidence", pid, handle
            )
            return
        self._completion._finish(
            handle, result_text, error_code=error_code, state=state, raw_result=raw_result
        )

    async def _rescue_jobs(
        self,
        pid: str,
        observer: HarnessObserver,
        clock: QuietClock,
        *,
        registration: LiveRegistration | None = None,
        turns: TurnAccumulator | None = None,
        turn_state: NativeTurnState | None = None,
    ) -> bool:
        """Run one rescue step; True while an armed confirmation is still to come."""
        if self._live_completion_owned(pid, registration):
            # Screen rescue is a heuristic; live-wired jobs finish through
            # exact terminal evidence only.
            logger.debug("live-wired %s: screen rescue suppressed for exact evidence", pid)
            return False
        if self.store.terminal_bindings.get(pid) is None:
            return False
        return await self._completion.rescue_jobs(
            pid,
            observer,
            clock,
            rescue_timeout=self.rescue,
            capture_fn=self._capture,
            now=self._monotonic(),
            turns=turns,
            turn_state=turn_state,
        )

    def _restored_turns(self, pid: str) -> TurnAccumulator:
        """Fresh conversation state, holding the turn fences that outlive watchers and restarts."""
        turns = TurnAccumulator()
        turns.remember(self.store.turn_fences.keys(pid))
        return turns

    def _note_turn_fences(self, pid: str, turns: TurnAccumulator, state: NativeTurnState) -> None:
        """Fence, durably, each turn started from a fenced queue item, before its batch applies."""
        promoted = turns.note_origins(state)
        if not promoted:
            return
        try:
            with self.store.write_unit() as unit:
                self.store.turn_fences.add(
                    pid, promoted, job_handle=None, created_at=now(), connection=unit.connection
                )
        except Exception:
            logger.exception("persisting turn fences for %s failed", pid)

    async def _release_unproven(
        self,
        pid: str,
        turns: TurnAccumulator,
        state: NativeTurnState,
        registration: LiveRegistration | None = None,
    ) -> None:
        if self._live_completion_owned(pid, registration):
            logger.debug("live-wired %s: unproven-history release suppressed", pid)
            return
        await self._completion.release_unproven(pid, state, turns)

    def _answer_turn(
        self,
        pid: str,
        result_text: str,
        heard: Sequence[str] = (),
        *,
        raw_result: str | object | None = RAW_RESULT_UNSET,
        registration: LiveRegistration | None = None,
        terminal: TurnTerminal | None = None,
        connection: Connection | None = None,
    ) -> None:
        if self._live_completion_owned(pid, registration):
            # Live-wired turns complete through exact terminal evidence via
            # the control service, never through heuristic text matching. A
            # source-bound registration keeps this suppression in force while
            # an old watcher is being cancelled after replace/unregister.
            logger.debug(
                "live-wired %s: heuristic turn answering suppressed for exact evidence", pid
            )
            return
        self._completion.answer_turn(
            pid,
            result_text,
            heard,
            raw_result=raw_result,
            terminal=terminal,
            connection=connection,
        )

    def _release_jobs(
        self,
        pid: str,
        result_text: str,
        *,
        error_code: str | None = None,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> None:
        if self._live_completion_owned(pid):
            logger.debug("live-wired %s: heuristic job release suppressed", pid)
            return
        self._completion.release_jobs(
            pid, result_text, error_code=error_code, raw_result=raw_result
        )

    def _finish_identity_lost_jobs(self, pid: str, result_text: str) -> None:
        if self._live_completion_owned(pid):
            # Identity loss is a durable-side heuristic; live wiring keeps
            # exact terminal evidence as the only completion authority.
            logger.debug("live-wired %s: identity-loss finish suppressed", pid)
            return
        self._completion.finish_identity_lost_jobs(pid, result_text)

    def _live_completion_owned(
        self, participant_id: str, registration: LiveRegistration | None = None
    ) -> bool:
        current = registration or self.live.registration_for(participant_id)
        return current is not None and current.channel.drives_job_completion
