"""Job completion and unmatched-turn tracking.

Resolves running jobs against turn boundaries and rescue windows. The
distinction between oldest-only normal completion and all-jobs rescue lives
here, along with the unmatched-prompt accounting that releases a job whose
prompt was never delivered.
"""

from __future__ import annotations

import logging

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


def answer_turn(
    pid: str,
    result_text: str,
    heard,
    *,
    store,
    jobs,
    unmatched: dict[str, int],
    raw_result: str | object | None = RAW_RESULT_UNSET,
    finish_fn,
) -> None:
    """One turn ended: hand its text to the one job that was waiting for it.

    The oldest running job, and only that one. Prompts arrive at a pane in
    the order they were typed and the agent works through them in that order,
    so turn N answers prompt N.

    ``heard`` is checked against the job's prompt before the job is resolved.
    A turn that does not answer the waiting job leaves it running, up to
    ``UNMATCHED_LIMIT`` consecutive misses.
    """
    if jobs is None:
        return
    job = store.oldest_running_job_for_target(pid)
    if job is None:
        return
    if not answers_prompt(heard, job.prompt):
        missed = unmatched.get(job.handle, 0) + 1
        unmatched[job.handle] = missed
        while len(unmatched) > UNMATCHED_CAP:
            unmatched.pop(next(iter(unmatched)))
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
        finish_fn(
            job.handle,
            "",
            error_code=UNDELIVERED_CODE,
            state=JobState.CRASHED,
            raw_result=None,
        )
        return
    unmatched.pop(job.handle, None)
    finish_fn(job.handle, result_text, raw_result=raw_result)


def release_jobs(
    pid: str,
    result_text: str,
    *,
    store,
    jobs,
    error_code: str | None = None,
    raw_result: str | object | None = RAW_RESULT_UNSET,
    finish_fn,
) -> None:
    """Finish *every* running job for this participant. Rescue only.

    The counterpart to ``answer_turn``. Rescue fires when no turn end was
    ever observed, so there is no boundary left to match a job to.
    """
    if jobs is None:
        return
    for job in store.running_jobs_for_target(pid):
        finish_fn(
            job.handle,
            result_text,
            error_code=error_code,
            raw_result=raw_result,
        )


def rescue_jobs(
    pid: str,
    observer_obj,
    clock,
    *,
    store,
    jobs,
    rescue_timeout: float,
    capture_fn,
    finish_fn,
) -> None:
    """Finish a job whose turn end was never read, so the caller unblocks.

    Deliberately narrow. Only ``ScreenKind.PROMPT`` triggers rescue:
    ``APPROVAL``/``TRUST`` mean the agent is blocked on a modal, not that a
    turn ended. Status is left alone: ``_check_idle_screen`` has already had
    its say at a much shorter timeout.
    """
    from theater.harness import ScreenKind

    if jobs is None or not store.running_jobs_for_target(pid):
        return
    p = store.get_participant(pid)
    if p is None or not p.tmux_pane:
        return
    capture = capture_fn(p.tmux_pane)
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
    release_jobs(
        pid,
        clock.last_text,
        error_code=RESCUE_CODE,
        raw_result=None,
        finish_fn=finish_fn,
        store=store,
        jobs=jobs,
    )


def finish_identity_lost_jobs(
    pid: str,
    result_text: str,
    *,
    store,
    jobs,
    finish_fn,
) -> None:
    """Finish all running jobs with the identity-lost error code.

    Retained for compatibility; may appear unused but is part of the public
    observer surface.
    """
    from theater.transcript_identity import TRANSCRIPT_IDENTITY_LOST_CODE

    if jobs is None:
        return
    for job in store.running_jobs_for_target(pid):
        finish_fn(
            job.handle,
            result_text,
            error_code=TRANSCRIPT_IDENTITY_LOST_CODE,
            state=JobState.CRASHED,
            raw_result=None,
        )
