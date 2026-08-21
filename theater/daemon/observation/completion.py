"""Job completion and unmatched-turn tracking.

``CompletionTracker`` owns the per-job miss counter and the answer/release/
rescue/finish methods. The distinction between oldest-only normal completion
and all-jobs rescue lives here, along with the unmatched-prompt accounting.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from theater.constants.observation import (
    RAW_RESULT_UNSET,
    RESCUE_CODE,
    UNDELIVERED_CODE,
    UNMATCHED_CAP,
    UNMATCHED_LIMIT,
)
from theater.daemon.observation.turns import answers_prompt
from theater.models import JobState

logger = logging.getLogger("theater.observer")


class CompletionTracker:
    """Owns _unmatched and the answer/release/rescue/finish decision tree."""

    def __init__(self, store, registry, *, jobs_fn):
        self.store = store
        self.registry = registry
        self._jobs_fn = jobs_fn
        self._unmatched: dict[str, int] = {}

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
    ) -> None:
        """One turn ended: hand its text to the one job that was waiting for it.

        The oldest running job, and only that one. Prompts arrive in the order
        they were typed, so turn N answers prompt N. A turn that does not answer
        the waiting job leaves it running, up to UNMATCHED_LIMIT consecutive misses.
        """
        if self.jobs is None:
            return
        job = self.store.oldest_running_job_for_target(pid)
        if job is None:
            return
        if not answers_prompt(heard, job.prompt):
            missed = self._unmatched.get(job.handle, 0) + 1
            self._unmatched[job.handle] = missed
            while len(self._unmatched) > UNMATCHED_CAP:
                self._unmatched.pop(next(iter(self._unmatched)))
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
            )
            return
        self._unmatched.pop(job.handle, None)
        self._finish(job.handle, result_text, raw_result=raw_result)

    def release_jobs(
        self,
        pid: str,
        result_text: str,
        *,
        error_code: str | None = None,
        raw_result: str | object | None = RAW_RESULT_UNSET,
    ) -> None:
        """Finish every running job for this participant. Rescue only."""
        if self.jobs is None:
            return
        for job in self.store.running_jobs_for_target(pid):
            self._finish(
                job.handle,
                result_text,
                error_code=error_code,
                raw_result=raw_result,
            )

    async def rescue_jobs(
        self,
        pid: str,
        observer_obj,
        clock,
        *,
        rescue_timeout: float,
        capture_fn,
    ) -> None:
        """Finish a job whose turn end was never read, so the caller unblocks.

        Deliberately narrow: only ScreenKind.PROMPT triggers rescue. Status is
        left alone — _check_idle_screen has already had its say.
        """
        from theater.harness import ScreenKind

        if self.jobs is None or not self.store.running_jobs_for_target(pid):
            return
        p = self.store.get_participant(pid)
        if p is None or not p.tmux_pane:
            return
        capture = await capture_fn(p.tmux_pane)
        if capture is None:
            return
        # Only a bare PROMPT justifies rescue.
        if observer_obj.screen_reading(capture).kind is not ScreenKind.PROMPT:
            return
        logger.warning(
            "no turn end seen for %s after %.0fs of quiet; finishing its jobs",
            pid,
            rescue_timeout,
        )
        self.release_jobs(
            pid,
            clock.last_text,
            error_code=RESCUE_CODE,
            raw_result=None,
        )

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
    ) -> None:
        """Resolve one job. The result is already clipped by the parser."""
        assert self.jobs is not None
        self._unmatched.pop(handle, None)
        if raw_result is RAW_RESULT_UNSET:
            self.jobs.finish(
                handle,
                state=state,
                result=result_text or "",
                error_code=error_code,
            )
        else:
            self.jobs.finish(
                handle,
                state=state,
                result=result_text or "",
                error_code=error_code,
                raw_result=raw_result,
            )
