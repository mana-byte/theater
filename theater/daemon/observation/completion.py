"""Job completion and unmatched-turn tracking.

Owns the split between oldest-only normal completion and all-jobs rescue.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Connection

from theater.constants.observability import AGENT_RESULT_INTERRUPTED
from theater.constants.observation import (
    COMPLETION_UNVERIFIED_CODE,
    RAW_RESULT_UNSET,
    RESCUE_CODE,
    TURN_FAILED_CODE,
    UNDELIVERED_CODE,
    UNMATCHED_CAP,
    UNMATCHED_LIMIT,
)
from theater.daemon.observation.rescue import RescueVerdict, confirmation_due, rescue_verdict
from theater.daemon.observation.turns import TurnAccumulator, answers_prompt, fence_keys
from theater.daemon.persistence.transactions import active_write_unit, after_commit
from theater.harness import TurnTerminal
from theater.harness.contracts.source import NativeTurnState
from theater.models import JobState, now

logger = logging.getLogger("theater.observer")

#: The transcript arm's twin of controls' ``_JOB_STATE_FOR_TERMINAL``: the
#: boundary event's outcome decides how the answered job finishes.
_FINISHED_FOR_TERMINAL: dict[TurnTerminal | None, tuple[JobState, str | None]] = {
    None: (JobState.DONE, None),
    TurnTerminal.INTERRUPTED: (JobState.KILLED, AGENT_RESULT_INTERRUPTED),
    TurnTerminal.FAILED: (JobState.CRASHED, TURN_FAILED_CODE),
    TurnTerminal.COMPLETED: (JobState.DONE, None),
}


class CompletionTracker:
    """Owns _unmatched and the answer/release/rescue/finish decision tree."""

    def __init__(self, store, registry, *, jobs_fn):
        self.store = store
        self.registry = registry
        self._jobs_fn = jobs_fn
        self._unmatched: dict[str, int] = {}
        #: Misses counted inside one open write unit, committed to _unmatched after it lands.
        self._unit_misses: tuple[object, dict[str, int]] | None = None

    @property
    def jobs(self):
        return self._jobs_fn()

    def answer_turn(
        self,
        pid: str,
        result_text: str,
        heard: Sequence[str] = (),
        *,
        raw_result: str | object | None = RAW_RESULT_UNSET,
        terminal: TurnTerminal | None = None,
        connection: Connection | None = None,
    ) -> None:
        """One turn ended: hand its text to the oldest delivered job, and only that one.

        Prompts arrive in typed order so turn N answers prompt N; a non-matching turn leaves
        the job running, up to UNMATCHED_LIMIT consecutive misses. An undelivered queued
        followup is not a candidate: no result and no unmatched counting before dispatch.
        """
        if self.jobs is None:
            return
        delivered = self.store.completion_candidate_jobs_for_target(pid, connection=connection)
        job = delivered[0] if delivered else None
        if job is None:
            return
        if not answers_prompt(heard, job.prompt):
            missed = self._count_miss(job.handle, connection)
            if missed < UNMATCHED_LIMIT:
                logger.info(
                    "turn at %s replies to something else; %s keeps waiting",
                    pid,
                    job.handle,
                )
                return
            logger.warning(
                "%s saw %d turns at %s answer someone else; its prompt never reached the queue",
                job.handle,
                missed,
                pid,
            )
            self._finish(
                job.handle,
                "",
                error_code=UNDELIVERED_CODE,
                state=JobState.CRASHED,
                raw_result=None,
                connection=connection,
            )
            return
        state, error_code = _FINISHED_FOR_TERMINAL[terminal]
        self._finish(
            job.handle,
            result_text,
            error_code=error_code,
            state=state,
            raw_result=raw_result,
            connection=connection,
        )

    def _count_miss(self, handle: str, connection: Connection | None) -> int:
        unit = active_write_unit(connection)
        if unit is None:
            self._remember_miss(handle, self._unmatched.get(handle, 0) + 1)
            return self._unmatched[handle]
        if self._unit_misses is None or self._unit_misses[0] is not unit:
            self._unit_misses = (unit, {})
        pending = self._unit_misses[1]
        missed = pending.get(handle, self._unmatched.get(handle, 0)) + 1
        pending[handle] = missed
        unit.after_commit(lambda: self._remember_miss(handle, missed))
        return missed

    def _forget_misses(self, handle: str) -> None:
        self._unmatched.pop(handle, None)

    def _remember_miss(self, handle: str, missed: int) -> None:
        self._unmatched[handle] = missed
        while len(self._unmatched) > UNMATCHED_CAP:
            self._unmatched.pop(next(iter(self._unmatched)))

    def release_jobs(
        self,
        pid: str,
        result_text: str,
        *,
        error_code: str | None = None,
        state: JobState = JobState.DONE,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> bool:
        """Finish every delivered running job for this participant; True when none was skipped."""
        if self.jobs is None:
            return False
        released = True
        for job in self.store.completion_candidate_jobs_for_target(pid):
            released &= self._finish(
                job.handle,
                result_text,
                error_code=error_code,
                state=state,
                raw_result=raw_result,
            )
        return released

    async def rescue_jobs(
        self,
        pid: str,
        observer_obj,
        clock,
        *,
        rescue_timeout: float,
        capture_fn,
        now: float | None = None,
        turns: TurnAccumulator | None = None,
        turn_state: NativeTurnState | None = None,
    ) -> bool:
        """Finish a job whose turn end was never read; True while a confirmation is still due.

        A bare PROMPT capture only arms; a later tick, after its own quiet source read, must
        confirm it. Status is left alone — _check_idle_screen has already had its say.
        """
        from theater.harness import ScreenKind

        if self.jobs is None or not self.store.completion_candidate_jobs_for_target(pid):
            clock.rescue_armed_at = None
            return False
        if self.store.get_participant(pid) is None:
            return False
        now = time.monotonic() if now is None else now
        armed_at = clock.rescue_armed_at
        if not confirmation_due(armed_at, now):
            return True
        capture = await capture_fn(pid)
        if capture is None:
            return False  # an unreadable capture decides nothing
        verdict = rescue_verdict(
            armed_at=armed_at,
            now=now,
            prompt=observer_obj.screen_reading(capture).kind is ScreenKind.PROMPT,
            turn=turn_state,
        )
        if verdict is RescueVerdict.ARM:
            clock.rescue_armed_at = now
            logger.info(
                "idle prompt at %s after %.0fs of quiet; confirming first", pid, rescue_timeout
            )
            return True
        if verdict is RescueVerdict.HOLD:
            logger.info("%s reports native work open over an idle prompt; rescue holds", pid)
            return False
        clock.rescue_armed_at = None
        if verdict is RescueVerdict.DECLINE:
            return False
        await self._rescue(pid, clock, verdict, rescue_timeout, turns, turn_state)
        return False

    async def _rescue(
        self,
        pid: str,
        clock,
        verdict: RescueVerdict,
        rescue_timeout: float,
        turns: TurnAccumulator | None,
        turn_state: NativeTurnState | None,
    ) -> None:
        assert self.jobs is not None
        pinned = await self.jobs.prepare_pins(pid)
        try:
            if verdict is RescueVerdict.SALVAGE:
                logger.warning(
                    "no turn end seen for %s after %.0fs of quiet; finishing its jobs",
                    pid,
                    rescue_timeout,
                )
                self.release_jobs(pid, clock.last_text, error_code=RESCUE_CODE, raw_result=None)
                return
            logger.warning(
                "%s still reports native turn %s open over an idle prompt; its jobs end "
                "unverified and that turn's late end will answer no one",
                pid,
                turn_state.turn_id if turn_state is not None else None,
            )
            self.end_unverified(pid, turn_state, turns)
        finally:
            self.jobs.release_pins(pinned)

    async def release_unproven(
        self, pid: str, turn_state: NativeTurnState, turns: TurnAccumulator | None
    ) -> None:
        """The source could not replay every turn end since its last read: fail closed."""
        if self.jobs is None or not self.store.completion_candidate_jobs_for_target(pid):
            return
        logger.warning(
            "%s: turn ends since the last read are not retained; its delivered jobs end "
            "unverified rather than take another turn's answer",
            pid,
        )
        pinned = await self.jobs.prepare_pins(pid)
        try:
            self.end_unverified(pid, turn_state, turns)
        finally:
            self.jobs.release_pins(pinned)

    def end_unverified(
        self, pid: str, turn_state: NativeTurnState | None, turns: TurnAccumulator | None
    ) -> None:
        """Crash delivered jobs without an answer; fence their open native work in the same unit."""
        assert self.jobs is not None
        keys = fence_keys(turn_state)
        with self.store.write_unit() as unit:
            candidates = self.store.completion_candidate_jobs_for_target(
                pid, connection=unit.connection
            )
            released = True
            for job in candidates:
                released &= self._finish(
                    job.handle,
                    "",
                    error_code=COMPLETION_UNVERIFIED_CODE,
                    state=JobState.CRASHED,
                    raw_result=None,
                    connection=unit.connection,
                )
            if not (released and candidates and keys):
                return
            self.store.turn_fences.add(
                pid,
                keys,
                job_handle=candidates[0].handle,
                created_at=now(),
                connection=unit.connection,
            )
            if turns is not None:
                unit.after_commit(lambda: turns.remember(keys))

    def finish_identity_lost_jobs(self, pid: str, result_text: str) -> None:
        """Finish all running jobs with the identity-lost error code."""
        from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

        if self.jobs is None:
            return
        for job in self.store.running_jobs_for_target(pid):
            self._finish(
                job.handle,
                result_text,
                error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
                state=JobState.CRASHED,
                raw_result=None,
            )

    def _finish(
        self,
        handle: str,
        result_text: str,
        *,
        error_code: str | None = None,
        state: JobState = JobState.DONE,
        raw_result: str | object | None = RAW_RESULT_UNSET,
        connection: Connection | None = None,
    ) -> bool:
        """Resolve one job. The result is already clipped by the parser."""
        assert self.jobs is not None
        if self.store.job_is_repairing(handle, connection=connection):
            # Rescue, identity loss and source errors are heuristics; a repair turn ends only
            # on its exact terminal evidence, so none of them may release its fence.
            logger.info("heuristic finish of %s skipped: it is awaiting an internal repair", handle)
            return False
        after_commit(connection, lambda: self._forget_misses(handle))
        extra: dict[str, Any] = {} if raw_result is RAW_RESULT_UNSET else {"raw_result": raw_result}
        if connection is not None:
            extra["connection"] = connection
        self.jobs.finish(
            handle, state=state, result=result_text or "", error_code=error_code, **extra
        )
        return True
