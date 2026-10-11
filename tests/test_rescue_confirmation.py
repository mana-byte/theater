"""Rescue arms on an idle prompt and ends a job only after a later, quiet source read."""

from __future__ import annotations

import logging

import pytest

from tests.test_observer import age_job, poised
from theater.constants.observation import (
    COMPLETION_UNVERIFIED_CODE,
    RESCUE_CODE,
    STALE_OPEN_TURN_SECONDS,
)
from theater.daemon.observation.service import _WatchContext
from theater.daemon.rpc.jobs import _job_to_dict
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.source import Batch, NativeTurnState, Source
from theater.models import JobState

#: Hint guidance makes every structured prompt share far more than the 120 matched characters.
GUIDANCE = "Answer with JSON only. " * 8
OPEN_A = NativeTurnState(turn_id="turn-a", open=True)


def said(text: str, turn_id: str | None = None, *, turn_end: bool = False) -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, turn_id=turn_id, turn_end=turn_end)


def heard(text: str, turn_id: str) -> Event:
    return Event(kind=EventKind.USER, text=text, turn_id=turn_id)


class Script(Source):
    """One queued batch per read, then quiet reads restating the native turn fact."""

    def __init__(self, state: NativeTurnState | None) -> None:
        self.state = state
        self.batches: list[Batch] = []

    async def read(self) -> Batch:
        return self.batches.pop(0) if self.batches else Batch(turn_state=self.state)


class Watch:
    """A watcher whose monotonic time the test sets before each ordinary tick."""

    def __init__(self, registry, *, state: NativeTurnState | None = None, **job) -> None:
        self.now = 0.0
        self.observer, screen, _, participant, self.jobs = poised(
            registry, monotonic_clock=lambda: self.now, poll=0.0, **job
        )
        self.observer.rescue = 60.0
        self.pid = participant.id
        age_job(registry, "h1", 3600)
        self.source = Script(state)
        self.context = _WatchContext(
            pid=self.pid,
            observer=screen,
            source=self.source,
            registration=None,
            opened_durable=True,
            finish_fn=self.observer._finish,
            wake=None,
        )

    async def tick(self, at: float, *batches: Batch) -> None:
        self.now = at
        self.source.batches.extend(batches)
        await self.observer._watch_iteration(self.context)

    async def quiet_until_armed(self, *earlier: Event) -> None:
        await self.tick(0.0, Batch(events=[*earlier, said("interim narration", "turn-a")]))
        await self.tick(1.0)
        await self.tick(62.0)  # sixty quiet seconds over an idle prompt: arm, never end
        await self.tick(63.0)  # inside the confirmation window: no capture, no decision
        assert self.context.clock.rescue_armed_at == 62.0
        assert self.jobs.get("h1").state == JobState.RUNNING


@pytest.mark.parametrize("final_lands", [True, False])
async def test_rescue_confirms_on_a_later_read_where_a_late_final_answer_wins(
    registry, caplog, final_lands
):
    """be1f903a008c#5911: the final record landed 88 ms before the capture that rescued."""
    watch = Watch(registry)
    await watch.quiet_until_armed()

    final = Batch(events=[said("FINAL", "turn-a", turn_end=True)])
    await watch.tick(64.5, *([final] if final_lands else []))

    job = watch.jobs.get("h1")
    if final_lands:
        assert (job.state, job.result, job.error_code) == (JobState.DONE, "FINAL", None)
    else:  # no authoritative turn fact: legacy salvage, labelled as possibly interim
        assert (job.state, job.result, job.error_code) == (
            JobState.DONE,
            "interim narration",
            RESCUE_CODE,
        )
        assert "may be interim" in _job_to_dict(job)["error"]
    assert "observing" not in caplog.text


async def test_open_native_work_is_never_salvaged_and_its_late_end_answers_no_newer_job(
    registry, caplog
):
    watch = Watch(registry, state=OPEN_A, prompt=GUIDANCE + "task A", response_format="json")
    await watch.quiet_until_armed(heard(GUIDANCE + "task A", "turn-a"))
    await watch.tick(64.5)  # confirmed, but the source still reports turn-a open: hold
    assert watch.jobs.get("h1").state == JobState.RUNNING

    await watch.tick(62.0 + STALE_OPEN_TURN_SECONDS + 1)
    job = watch.jobs.get("h1")
    assert (job.state, job.result, job.error_code, job.structured_status) == (
        JobState.CRASHED,
        "",
        COMPLETION_UNVERIFIED_CODE,
        "unavailable",
    )
    assert "could not be verified" in _job_to_dict(job)["error"]

    watch.jobs.create(
        handle="h2", caller_id="caller", target_id=watch.pid, kind="send", prompt=GUIDANCE + "B"
    )
    with caplog.at_level(logging.WARNING, logger="theater.observer"):
        await watch.tick(400.0, Batch(events=[said("late A", "turn-a", turn_end=True)]))
    assert watch.jobs.get("h2").state == JobState.RUNNING
    assert "late turn end" in caplog.text

    own_turn = [heard(GUIDANCE + "B", "turn-b"), said("B answer", "turn-b", turn_end=True)]
    await watch.tick(401.0, Batch(events=own_turn))
    assert watch.jobs.get("h2").result == "B answer"
    assert "observing" not in caplog.text
