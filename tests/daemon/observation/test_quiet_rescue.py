"""Quiet clocks and the rescue that ends a job whose turn end was never read."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from types import SimpleNamespace

import pytest

from tests.support.observation import age_job, apply_batch, poised, run_quiet_step
from tests.support.vibe_store import (
    A_ENDS,
    ANSWER_A,
    B_STARTS,
    GEN1,
    GEN2,
    RUNNING_A,
    SESSION_ID,
    USER_A,
    attached_to_running_a,
    public_message,
    turn_doc,
    turn_record,
)
from tests.support.vibe_store import Store as VibeStore
from theater.constants.observation import (
    COMPLETION_UNVERIFIED_CODE,
    RESCUE_CODE,
    RESCUE_CONFIRM_SECONDS,
    STALE_OPEN_TURN_SECONDS,
)
from theater.daemon.jobs import JobManager
from theater.daemon.observation.service import _WatchContext
from theater.daemon.observer import (
    AWAITING_INPUT_TIMEOUT,
    RELOCATE_TIMEOUT,
    RESCUE_TIMEOUT,
    Observer,
    QuietClock,
    TurnAccumulator,
)
from theater.daemon.persistence.repositories.turn_fences import TurnFenceRepository
from theater.daemon.rpc.jobs import _job_to_dict
from theater.harness.base import Event, EventKind
from theater.harness.builtin.plugins.vibe.observer import VibeObserver
from theater.harness.contracts.source import Batch, NativeTurnState, Source
from theater.models import (
    JobState,
)
from theater.provenance import TranscriptProvenance


def test_begin_quiet_starts_the_clocks_once_and_leaves_them_running():
    cursor = QuietClock()
    cursor.begin_quiet(100.0)
    cursor.begin_quiet(140.0)  # still the same silence
    assert cursor.quiet_for(140.0) == 40.0
    assert cursor.screen_quiet_for(140.0) == 40.0


def test_the_screen_clock_survives_a_relocate_resetting_nothing():
    """The v1 bug: one shared clock meant the screen check never fired.

    The relocate fires at 5s and the screen check shortly after. If the
    relocate reset the clock the screen check reads, the screen quiet
    never accumulates and AWAITING_INPUT is unreachable. Only the screen
    check may reset its own clock.
    """
    cursor = QuietClock()
    cursor.begin_quiet(0.0)

    # Two relocate windows go by. Nothing resets anything.
    assert cursor.quiet_for(6.0) > RELOCATE_TIMEOUT
    assert cursor.quiet_for(11.0) > RELOCATE_TIMEOUT

    # The screen check is still reachable, and throttles only itself.
    assert cursor.screen_quiet_for(11.0) > AWAITING_INPUT_TIMEOUT
    cursor.screen_quiet_since = 11.0
    assert cursor.screen_quiet_for(12.0) < AWAITING_INPUT_TIMEOUT
    assert cursor.quiet_for(12.0) == 12.0  # relocate clock untouched


def test_new_bytes_restart_both_clocks():
    cursor = QuietClock()
    cursor.begin_quiet(0.0)
    cursor.screen_quiet_since = 5.0
    cursor.stir()
    assert cursor.quiet_since is None and cursor.screen_quiet_since is None
    assert cursor.quiet_for(20.0) == 0.0


def test_raw_only_progress_preserves_the_screen_clock():
    cursor = QuietClock()
    cursor.begin_quiet(0.0)
    cursor.stir_raw()
    assert cursor.quiet_since is None
    assert cursor.rescue_since is None
    assert cursor.screen_quiet_for(20.0) == 20.0


def test_the_rescue_clock_survives_the_screen_check_throttling_itself():
    """The rescue must not read a clock that another timer keeps pushing.

    `_check_idle_screen` throttles itself by setting screen_quiet_since to now
    every time it fires. A rescue reading that clock would restart every
    `awaiting_input_timeout` and never reach its own, much longer, timeout —
    the same shape of bug as the relocate one above, but silent: the symptom
    is a caller that waits forever, not a status that never changes.
    """
    cursor = QuietClock()
    cursor.begin_quiet(0.0)

    # Enough screen windows to exceed the rescue timeout, each one
    # throttling the screen clock.
    t = 0.0
    while t <= RESCUE_TIMEOUT:
        t += AWAITING_INPUT_TIMEOUT + 0.5
        assert cursor.screen_quiet_for(t) > AWAITING_INPUT_TIMEOUT
        cursor.screen_quiet_since = t

    # The rescue clock has been counting that whole time regardless.
    assert cursor.rescue_quiet_for(t) == t
    assert cursor.rescue_quiet_for(t) > RESCUE_TIMEOUT


def test_new_bytes_restart_the_rescue_clock_too():
    cursor = QuietClock()
    cursor.begin_quiet(0.0)
    cursor.stir()
    assert cursor.rescue_since is None
    assert cursor.rescue_quiet_for(90.0) == 0.0


def expire_rescue_clock(clock: QuietClock, observer: Observer) -> None:
    clock.rescue_since = time.monotonic() - observer.rescue - 1


def let_rescue_confirm(clock: QuietClock) -> None:
    """Age the arming capture past the confirmation window; the next quiet check confirms."""
    assert clock.rescue_armed_at is not None
    clock.rescue_armed_at -= RESCUE_CONFIRM_SECONDS


async def rescue(observer: Observer, screen, clock: QuietClock, pid: str) -> None:
    """Rescue's two steps: an idle capture arms, a later one confirms."""
    assert await observer._rescue_jobs(pid, screen, clock)
    let_rescue_confirm(clock)
    assert not await observer._rescue_jobs(pid, screen, clock)


class QuietSource(Source):
    async def read(self) -> Batch:
        return Batch()


@pytest.mark.asyncio
async def test_rescue_waits_for_the_job_itself_to_age(registry):
    """A long-idle pane must not hand its old quiet time to a fresh job.

    Claude's JSONL records no stop reason, so an interrupted turn is rescued
    here as done with ``turn_end_unseen`` — never killed."""
    observer, screen, clock, p, jobs = poised(registry)
    observer.rescue = 60.0
    expire_rescue_clock(clock, observer)

    await run_quiet_step(observer, p.id, screen, QuietSource(), clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "running"
    assert job.error_code is None

    age_job(registry, "h1", observer.rescue + 1)
    await run_quiet_step(observer, p.id, screen, QuietSource(), clock, TurnAccumulator())
    assert str(jobs.get("h1").state) == "running"  # armed, not ended
    let_rescue_confirm(clock)
    await run_quiet_step(observer, p.id, screen, QuietSource(), clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.error_code == "turn_end_unseen"


@pytest.mark.asyncio
async def test_rescue_gate_uses_the_oldest_running_job(registry):
    """A fresh queued job must not keep an older wedged caller waiting."""
    observer, screen, clock, p, jobs = poised(registry)
    observer.rescue = 60.0
    age_job(registry, "h1", observer.rescue + 1)
    jobs.create(handle="h2", caller_id="other", target_id=p.id, kind="send")
    expire_rescue_clock(clock, observer)

    await run_quiet_step(observer, p.id, screen, QuietSource(), clock, TurnAccumulator())
    let_rescue_confirm(clock)
    await run_quiet_step(observer, p.id, screen, QuietSource(), clock, TurnAccumulator())
    assert str(jobs.get("h1").state) == "done"
    assert str(jobs.get("h2").state) == "done"


@pytest.mark.asyncio
async def test_rescue_gate_tolerates_no_running_jobs(registry):
    observer, screen, clock, p, jobs = poised(registry)
    observer._answer_turn(p.id, "done already")
    expire_rescue_clock(clock, observer)
    expired_since = clock.rescue_since

    await run_quiet_step(observer, p.id, screen, QuietSource(), clock, TurnAccumulator())
    assert jobs.get("h1").result == "done already"
    assert clock.rescue_since is not None
    assert expired_since is not None
    assert clock.rescue_since > expired_since


@pytest.mark.asyncio
async def test_rescue_attempts_are_still_throttled(registry):
    observer, screen, clock, p, jobs = poised(registry, idle=False)
    observer.rescue = 60.0
    age_job(registry, "h1", observer.rescue + 1)
    captures = 0

    async def capture_pane(_pane):
        nonlocal captures
        captures += 1
        return "$ "

    observer._capture = capture_pane
    expire_rescue_clock(clock, observer)

    await run_quiet_step(observer, p.id, screen, QuietSource(), clock, TurnAccumulator())
    await run_quiet_step(observer, p.id, screen, QuietSource(), clock, TurnAccumulator())

    assert captures == 1
    assert str(jobs.get("h1").state) == "running"


@pytest.mark.asyncio
async def test_a_quiet_participant_over_an_idle_screen_releases_its_caller(registry):
    observer, screen, cursor, p, jobs = poised(registry)
    await rescue(observer, screen, cursor, p.id)
    job = jobs.get("h1")
    # DONE, not a failure: the caller has a usable answer, and failing the job
    # would leave it blocked on exactly the thing being rescued. The error code
    # is what says this was salvaged rather than declared finished.
    assert str(job.state) == "done"
    assert job.result == "the last thing it said"
    assert job.error_code == "turn_end_unseen"


@pytest.mark.parametrize(
    "poise",
    [
        # Quiet plus a working screen is a slow tool call, not a missed boundary.
        pytest.param({"idle": False}, id="busy-screen"),
        pytest.param({"capture": None}, id="unreadable-screen"),
        # No terminal, no screen, no second opinion: silence alone decides nothing.
        pytest.param({"provider_bound": False}, id="no-pane"),
    ],
)
async def test_a_screen_that_cannot_confirm_idleness_rescues_nothing(registry, poise):
    observer, screen, cursor, p, jobs = poised(registry, **poise)
    await observer._rescue_jobs(p.id, screen, cursor)
    assert str(jobs.get("h1").state) == "running"


@pytest.mark.asyncio
async def test_a_turn_end_that_was_actually_read_carries_no_error_code(registry):
    observer, _screen, _cursor, p, jobs = poised(registry)
    observer._answer_turn(p.id, "a real reply")
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == "a real reply"
    assert job.error_code is None


@pytest.mark.asyncio
async def test_structured_rescue_is_unavailable(registry):
    observer, screen, cursor, p, jobs = poised(registry, response_format="json")
    cursor.last_text = '{"answer": 42}'
    await rescue(observer, screen, cursor, p.id)
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == '{"answer": 42}'
    assert job.error_code == "turn_end_unseen"
    assert job.structured_result is None
    assert job.structured_status == "unavailable"


def test_structured_screen_result_is_unavailable(registry):
    observer, _screen, _cursor, p, jobs = poised(registry, response_format="json")
    observer._end_turn_from_screen(p.id, '{"answer": 42}\n$ ')
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == '{"answer": 42}'
    assert job.structured_result is None
    assert job.structured_status == "unavailable"


def test_structured_error_boundary_is_unavailable(registry):
    observer, _screen, clock, p, jobs = poised(registry, response_format="json")
    batch = Batch(
        events=[
            Event(
                kind=EventKind.ERROR,
                text="null",
                raw_text="null",
                turn_end=True,
            )
        ]
    )
    apply_batch(observer, p.id, batch, clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == "null"
    assert job.structured_result is None
    assert job.structured_status == "unavailable"


@pytest.mark.asyncio
async def test_rescue_still_releases_every_waiting_caller(registry):
    """The backstop stays fan-out: nothing else will ever free the rest.

    Per-turn matching has already failed by the time rescue fires, so there is
    no boundary left to pair a job with. Releasing one at a time would drip the
    queue out over one rescue window each.
    """
    observer, screen, clock, p, jobs = poised(registry)
    jobs.create(handle="h2", caller_id="other", target_id=p.id, kind="send")
    await rescue(observer, screen, clock, p.id)
    assert str(jobs.get("h1").state) == "done"
    assert str(jobs.get("h2").state) == "done"


#: Hint guidance makes every structured prompt share far more than the 120 matched characters.
GUIDANCE = "Answer with JSON only. " * 8


OPEN_A = NativeTurnState(turn_id="turn-a", open=True)


def said(text: str, turn_id: str | None = None, *, turn_end: bool = False) -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, turn_id=turn_id, turn_end=turn_end)


def heard(text: str, turn_id: str) -> Event:
    return Event(kind=EventKind.USER, text=text, turn_id=turn_id)


class Script(Source):
    """One queued batch per read, then quiet reads restating the native turn fact.

    A queued batch is held until acknowledged; a rollback hands it out again.
    """

    def __init__(self, state: NativeTurnState | None) -> None:
        self.state = state
        self.batches: list[Batch] = []
        self._held: Batch | None = None

    async def read(self) -> Batch:
        if not self.batches:
            return Batch(turn_state=self.state)
        self._held = self.batches.pop(0)
        return self._held

    def pending_source_checkpoint(self) -> str | None:
        return None if self._held is None else "script"

    def acknowledge_source_checkpoint(self) -> None:
        self._held = None

    def rollback_source_checkpoint(self) -> None:
        if self._held is not None:
            self.batches.insert(0, self._held)
            self._held = None


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Watch:
    """A watcher whose monotonic time the test sets before each ordinary tick."""

    def __init__(self, observer: Observer, context: _WatchContext, jobs, clock: Clock) -> None:
        self.observer, self.context, self.jobs, self.clock = observer, context, jobs, clock
        self.pid = context.pid
        self.source = context.source

    @classmethod
    def poised(
        cls, registry, *, state: NativeTurnState | None = None, source: Source | None = None, **job
    ) -> Watch:
        clock = Clock()
        observer, screen, _, participant, jobs = poised(
            registry, monotonic_clock=clock, poll=0.0, **job
        )
        observer.rescue = 60.0
        age_job(registry, "h1", 3600)
        context = _WatchContext(
            pid=participant.id,
            observer=screen,
            source=Script(state) if source is None else source,
            registration=None,
            opened_durable=True,
            completion=observer._bind_completion(None),
            wake=None,
        )
        return cls(observer, context, jobs, clock)

    def restarted(self, registry, source_factory=None) -> Watch:
        """The same participant under a new daemon, whose watcher opens the production way."""
        script = Script(getattr(self.source, "state", None))
        jobs = JobManager(registry.store)
        observer = Observer(
            registry,
            harnesses={"vibe": SimpleNamespace(observer=self.context.observer)},
            jobs=jobs,
            monotonic_clock=self.clock,
            poll=0.0,
            source_factory=source_factory or (lambda *_args, **_kwargs: script),
        )
        observer._capture = self.observer._capture
        context = observer._prepare_watch(self.pid, "vibe")
        assert context is not None
        return Watch(observer, context, jobs, self.clock)

    async def tick(self, at: float, *batches: Batch) -> None:
        self.clock.now = at
        if batches:
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
    watch = Watch.poised(registry)
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


@pytest.mark.parametrize("restart", [False, True])
async def test_open_native_work_is_never_salvaged_and_its_late_end_answers_no_newer_job(
    registry, caplog, restart
):
    """The fence is durable: neither a new watcher nor a new daemon forgets it."""
    watch = Watch.poised(registry, state=OPEN_A, prompt=GUIDANCE + "task A", response_format="json")
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
    assert registry.store.turn_fences.keys(watch.pid) == ("turn-a",)

    if restart:
        watch = watch.restarted(registry)
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


async def test_unprovable_turn_history_ends_delivered_jobs_unverified(registry, caplog):
    """A source that cannot replay the turn ends behind a read fails closed: no guessed answer."""
    watch = Watch.poised(registry)
    gap = NativeTurnState(turn_id="turn-c", open=True, complete=False)

    await watch.tick(
        0.0, Batch(events=[said("B's answer", "turn-b", turn_end=True)], turn_state=gap)
    )

    job = watch.jobs.get("h1")
    assert (job.state, job.result, job.error_code) == (
        JobState.CRASHED,
        "",
        COMPLETION_UNVERIFIED_CODE,
    )
    assert registry.store.turn_fences.keys(watch.pid) == ("turn-c",)
    assert "observing" not in caplog.text


async def test_a_promoted_fence_is_durable_before_its_batch_applies(registry, caplog, monkeypatch):
    """A failed fence write leaves the batch unapplied and unacknowledged, so it is retried."""
    watch = Watch.poised(registry)
    with registry.store.write_unit() as unit:  # a crash fenced queued input q earlier
        registry.store.turn_fences.add(
            watch.pid, ["queue:q"], job_handle="h0", created_at=0.0, connection=unit.connection
        )
    watch.context.turns = watch.observer._restored_turns(watch.pid)
    born_of_q = NativeTurnState(turn_id="turn-b", origins=(("turn-b", "q"),))
    original_add = TurnFenceRepository.add
    failures = iter([OSError("disk full")])

    def flaky_add(self, *args, **kwargs):
        failure = next(failures, None)
        if failure is not None:
            raise failure
        return original_add(self, *args, **kwargs)

    monkeypatch.setattr(TurnFenceRepository, "add", flaky_add)
    late_b = Batch(events=[said("B's end", "turn-b", turn_end=True)], turn_state=born_of_q)

    await watch.tick(0.0, late_b)
    assert "observing" in caplog.text  # the batch failed whole: nothing applied or acknowledged
    assert not watch.context.turns.is_fenced("turn-b")
    assert watch.jobs.get("h1").state == JobState.RUNNING

    await watch.tick(1.0)  # the source hands the same batch out again
    assert watch.jobs.get("h1").state == JobState.RUNNING
    assert registry.store.turn_fences.keys(watch.pid) == ("queue:q", "turn-b")


async def test_an_expired_checkpoint_fails_closed_on_its_attachment_across_a_restart(
    registry, tmp_path
):
    """Old job A, current turn C: after a restart C's end must not complete A, or a newer job."""
    vibe = VibeStore(tmp_path)
    expired = (await asyncio.to_thread(attached_to_running_a, vibe)).source_checkpoint()
    user_c = public_message("user-c", "user", "job C prompt", turn_id="turn-c")
    running_c = turn_doc([USER_A, ANSWER_A, user_c], "turn-c", "in_progress")
    vibe.publish(generation=GEN2, snapshot_sequence=3, state=running_c, watermark=4)
    shutil.rmtree(vibe.generation_dir(GEN1))  # the history behind the checkpoint is gone

    def unified(checkpoint):
        return VibeObserver(root=vibe.session_root.parent.parent).open_source(
            cwd="/tmp/work",
            session_id=SESSION_ID,
            session_provenance=TranscriptProvenance.EXACT,
            known_location=str(vibe.current),
            source_checkpoint=checkpoint,
        )

    watch = Watch.poised(registry, source=unified(expired), prompt=GUIDANCE + "A")
    await watch.tick(0.0)  # the attachment itself reports the gap; A ends before it can advance
    job = watch.jobs.get("h1")
    assert (job.state, job.result, job.error_code) == (
        JobState.CRASHED,
        "",
        COMPLETION_UNVERIFIED_CODE,
    )
    assert registry.store.turn_fences.keys(watch.pid) == ("turn-c",)

    watch = watch.restarted(
        registry, source_factory=lambda *_, **kwargs: unified(kwargs["source_checkpoint"])
    )
    watch.jobs.create(
        handle="h2", caller_id="caller", target_id=watch.pid, kind="send", prompt=GUIDANCE + "B"
    )
    answer_c = public_message("asst-c", "assistant", "ANSWER C", turn_id="turn-c")
    vibe.publish(
        generation=GEN2,
        snapshot_sequence=3,
        state=running_c,
        watermark=4,
        journal=[turn_record(5, "turn-c", "completed", answer_c)],
    )
    await watch.tick(1.0)  # attach from the checkpoint the first daemon advanced
    await watch.tick(2.0)  # C's end
    assert watch.jobs.get("h1").state == JobState.CRASHED
    assert watch.jobs.get("h2").state == JobState.RUNNING


async def test_a_journal_moving_backwards_never_salvages_the_open_turn(registry, tmp_path):
    """Truncation behind acknowledged open B: B's job fails closed, the checkpoint stays put."""
    vibe = VibeStore(tmp_path)
    source = await asyncio.to_thread(attached_to_running_a, vibe)
    vibe.publish(
        generation=GEN1,
        snapshot_sequence=0,
        state=RUNNING_A,
        watermark=1,
        journal=[A_ENDS, B_STARTS],
    )
    await source.read()
    source.acknowledge_source_checkpoint()
    acknowledged = source.source_checkpoint()
    watch = Watch.poised(registry, source=source, prompt=GUIDANCE + "B")
    vibe.publish(
        generation=GEN1, snapshot_sequence=0, state=RUNNING_A, watermark=1, journal=[A_ENDS]
    )

    await watch.tick(0.0)

    job = watch.jobs.get("h1")
    assert (job.state, job.error_code) == (JobState.CRASHED, COMPLETION_UNVERIFIED_CODE)
    assert watch.context.turn_state is not None and watch.context.turn_state.turn_id == "turn-b"
    assert source.source_checkpoint() == acknowledged
    assert registry.store.get_participant(watch.pid).source_checkpoint in (None, acknowledged)
