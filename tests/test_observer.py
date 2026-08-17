"""Tailing a live transcript into the bus.

Everything here runs against a temporary Vibe-shaped tree, never the real
~/.vibe. The harness is constructed with an explicit root for exactly that
reason.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from shipped import VibeHarness
from sqlalchemy import update

from theater.daemon import methods as methods_mod
from theater.daemon import observer as observer_mod
from theater.daemon.jobs import JobManager
from theater.daemon.observer import (
    AWAITING_INPUT_TIMEOUT,
    RELOCATE_TIMEOUT,
    RESCUE_TIMEOUT,
    Observer,
    QuietClock,
    Turn,
    TurnAccumulator,
)
from theater.daemon.schema import jobs as jobs_table
from theater.harness.base import Event, EventKind
from theater.harness.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.source import Attachment, Batch, Source
from theater.models import JobState, Status, now

USER = {"role": "user", "content": "do the thing"}
WORKING = {
    "role": "assistant",
    "content": "on it",
    "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}],
}
RESULT = {"role": "tool", "content": "output", "name": "bash", "tool_call_id": "c1"}
DONE = {"role": "assistant", "content": "finished"}


@pytest.fixture
def vibe_tree(tmp_path):
    """A Vibe log root with one session whose cwd is a temporary project."""
    root = tmp_path / "logs" / "session"
    session = root / "session_20260101_000000_deadbeef"
    session.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    (session / "meta.json").write_text(
        json.dumps(
            {
                "session_id": "deadbeef-1111-2222-3333",
                "environment": {"working_directory": str(project)},
            }
        )
    )
    transcript = session / "messages.jsonl"
    transcript.write_text("")
    return {"root": root, "project": project, "transcript": transcript}


def append(path: Path, *records: dict) -> None:
    with path.open("a") as fh:
        for record in records:
            fh.write(json.dumps(record) + "\n")
        fh.flush()


async def until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


@pytest.fixture
async def observing(registry, vibe_tree):
    """An observer wound tight enough to finish inside a test."""
    observer = Observer(
        registry,
        {"vibe": VibeHarness(root=vibe_tree["root"])},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    observer.start()
    yield observer
    await observer.aclose()


def kinds(store, prefix="agent."):
    return [row["kind"] for row in store.bus_tail(limit=500) if row["kind"].startswith(prefix)]


async def test_new_records_reach_the_bus_as_normalized_events(registry, vibe_tree, observing):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in kinds(registry.store))

    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)

    assert await until(lambda: "agent.assistant" in kinds(registry.store))
    assert kinds(registry.store) == [
        "agent.transcript",
        "agent.user",
        "agent.assistant",
        "agent.tool_call",
        "agent.tool_result",
        "agent.assistant",
    ]
    rows = [r for r in registry.store.bus_tail(limit=500) if r["kind"] == "agent.tool_call"]
    assert rows[0]["payload"]["tool"] == "bash"
    assert rows[0]["from_id"] == p.id
    # Vibe keeps no clock of its own; the payload says so rather than lying.
    assert rows[0]["payload"]["ts"] is None
    assert rows[0]["ts"] > 0


async def test_status_follows_the_turn_boundary(registry, vibe_tree, observing):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in kinds(registry.store))

    append(vibe_tree["transcript"], USER, WORKING)
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)

    append(vibe_tree["transcript"], RESULT, DONE)
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)


async def test_attaching_skips_history_instead_of_replaying_it(registry, vibe_tree, observing):
    """An adopted agent can have megabytes behind it. None of it is news."""
    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    await asyncio.sleep(0.1)
    assert kinds(registry.store) == ["agent.transcript"]

    attach = next(r for r in registry.store.bus_tail(limit=500) if r["kind"] == "agent.transcript")
    assert attach["payload"]["skipped_records"] == 4

    append(vibe_tree["transcript"], USER)
    assert await until(lambda: "agent.user" in kinds(registry.store))


async def test_attaching_derives_status_from_the_last_skipped_record(
    registry, vibe_tree, observing
):
    """A spawned agent that finished before we attached must not stay idle.

    The bus gets no history replayed, but the status must reflect the last
    record seen at attach time. This is the bug that left every spawned
    participant stuck at "idle" in the real run: the agent completed its
    turn in the 2 seconds it took the observer to find the transcript, no new
    bytes arrived after attach, and _drain never fired.
    """
    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)
    # The bus carries only the transcript attach event — no history replay.
    assert kinds(registry.store) == ["agent.transcript"]


async def test_attaching_to_a_working_agent_sets_working(registry, vibe_tree, observing):
    """If the last record is a tool call (no turn_end), status is WORKING."""
    append(vibe_tree["transcript"], USER, WORKING)
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)


async def test_the_harness_session_id_is_recorded_on_the_participant(
    registry, vibe_tree, observing
):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: registry.get(p.id).session_id == "deadbeef-1111-2222-3333")


async def test_a_half_written_record_is_not_parsed_until_it_is_complete(
    registry, vibe_tree, observing
):
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in kinds(registry.store))

    with vibe_tree["transcript"].open("a") as fh:
        fh.write(json.dumps(USER)[:20])
        fh.flush()
    await asyncio.sleep(0.1)
    assert kinds(registry.store) == ["agent.transcript"]

    with vibe_tree["transcript"].open("a") as fh:
        fh.write(json.dumps(USER)[20:] + "\n")
        fh.flush()
    assert await until(lambda: "agent.user" in kinds(registry.store))


async def test_a_truncated_transcript_is_re_read_from_the_top(registry, vibe_tree, observing):
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    append(vibe_tree["transcript"], USER)
    assert await until(lambda: kinds(registry.store).count("agent.user") == 1)

    vibe_tree["transcript"].write_text("")
    append(vibe_tree["transcript"], USER)
    assert await until(lambda: kinds(registry.store).count("agent.user") == 2)


async def test_a_dead_participant_stops_being_watched(registry, vibe_tree, observing):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: p.id in observing._tasks)

    registry.mark_dead(p.id)
    assert await until(lambda: p.id not in observing._tasks)

    append(vibe_tree["transcript"], USER)
    await asyncio.sleep(0.1)
    assert "agent.user" not in kinds(registry.store)


async def test_participants_of_an_unknown_harness_are_left_alone(registry, vibe_tree):
    observer = Observer(registry, {}, poll=0.01, search=0.01, sync=0.01)
    observer.start()
    try:
        registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
        await asyncio.sleep(0.1)
        assert observer._tasks == {}
        assert kinds(registry.store) == []
    finally:
        await observer.aclose()


async def test_an_unobservable_participant_is_reported_once(registry, vibe_tree, observing, caplog):
    """A misreported harness name must not become a silent blind spot."""
    caplog.set_level("WARNING", logger="theater.observer")
    registry.register(harness="claude_code", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(lambda: "cannot observe" in caplog.text)
    await asyncio.sleep(0.1)
    assert caplog.text.count("cannot observe") == 1


async def test_an_observer_with_no_transcript_waits_without_spinning_out(registry, tmp_path):
    """A participant we cannot find a transcript for is not an error."""
    observer = Observer(
        registry,
        {"vibe": VibeHarness(root=tmp_path / "empty")},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    observer.start()
    try:
        p = registry.register(harness="vibe", pane=None, cwd=str(tmp_path))
        assert await until(lambda: p.id in observer._tasks)
        await asyncio.sleep(0.1)
        assert not observer._tasks[p.id].done()
        assert kinds(registry.store) == []
    finally:
        await observer.aclose()


# ---- the quiet clock --------------------------------------------------


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


# ---- rescuing a job whose turn end was never read ---------------------


class ScreenObserver(HarnessObserver):
    """Just enough observer for `_rescue_jobs`, which reads `screen_reading`.

    Inherits the default `screen_reading` shim, which derives a reading from
    `is_idle_screen`. An observer and not a harness: rescue is observation,
    so the reducer is handed only that half and never sees the object that
    launches anything.
    """

    has_transcript = True

    def __init__(self, idle: bool):
        self.idle = idle

    def is_idle_screen(self, capture: str) -> bool:
        return self.idle


def poised(
    registry,
    *,
    pane="%1",
    idle=True,
    capture="$ ",
    prompt: str | None = None,
    response_format: str | None = None,
):
    """A participant with one running job, and an observer poised to rescue it.

    `capture=None` stands for a pane that could not be read at all.
    """
    from theater.daemon.jobs import JobManager

    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane=pane, cwd="/tmp")
    jobs.create(
        handle="h1",
        caller_id="caller",
        target_id=p.id,
        kind="send",
        prompt=prompt,
        response_format=response_format,
    )
    observer = Observer(registry, harnesses={}, jobs=jobs)

    async def capture_pane(_pane):
        return capture

    observer._capture = capture_pane
    cursor = QuietClock()
    cursor.last_text = "the last thing it said"
    return observer, ScreenObserver(idle), cursor, p, jobs


def expire_rescue_clock(clock: QuietClock, observer: Observer) -> None:
    clock.rescue_since = time.monotonic() - observer.rescue - 1


def age_job(registry, handle: str, age: float) -> None:
    registry.store.conn.execute(
        update(jobs_table).where(jobs_table.c.handle == handle).values(created_at=now() - age)
    )


class QuietSource(Source):
    async def read(self) -> Batch:
        return Batch()


@pytest.mark.asyncio
async def test_rescue_waits_for_the_job_itself_to_age(registry):
    """A long-idle pane must not hand its old quiet time to a fresh job."""
    observer, screen, clock, p, jobs = poised(registry)
    observer.rescue = 60.0
    expire_rescue_clock(clock, observer)

    await observer._on_quiet(p.id, screen, QuietSource(), clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "running"
    assert job.error_code is None

    age_job(registry, "h1", observer.rescue + 1)
    await observer._on_quiet(p.id, screen, QuietSource(), clock, TurnAccumulator())
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

    await observer._on_quiet(p.id, screen, QuietSource(), clock, TurnAccumulator())
    assert str(jobs.get("h1").state) == "done"
    assert str(jobs.get("h2").state) == "done"


@pytest.mark.asyncio
async def test_rescue_gate_tolerates_no_running_jobs(registry):
    observer, screen, clock, p, jobs = poised(registry)
    observer._answer_turn(p.id, "done already")
    expire_rescue_clock(clock, observer)
    expired_since = clock.rescue_since

    await observer._on_quiet(p.id, screen, QuietSource(), clock, TurnAccumulator())
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

    await observer._on_quiet(p.id, screen, QuietSource(), clock, TurnAccumulator())
    await observer._on_quiet(p.id, screen, QuietSource(), clock, TurnAccumulator())

    assert captures == 1
    assert str(jobs.get("h1").state) == "running"


@pytest.mark.asyncio
async def test_a_quiet_participant_over_an_idle_screen_releases_its_caller(registry):
    observer, screen, cursor, p, jobs = poised(registry)
    await observer._rescue_jobs(p.id, screen, cursor)
    job = jobs.get("h1")
    # DONE, not a failure: the caller has a usable answer, and failing the job
    # would leave it blocked on exactly the thing being rescued. The error code
    # is what says this was salvaged rather than declared finished.
    assert str(job.state) == "done"
    assert job.result == "the last thing it said"
    assert job.error_code == "turn_end_unseen"


@pytest.mark.asyncio
async def test_a_busy_screen_is_not_rescued(registry):
    """Quiet plus a working screen is a slow tool call, not a missed boundary."""
    observer, screen, cursor, p, jobs = poised(registry, idle=False)
    await observer._rescue_jobs(p.id, screen, cursor)
    assert str(jobs.get("h1").state) == "running"


@pytest.mark.asyncio
async def test_an_unreadable_screen_decides_nothing(registry):
    observer, screen, cursor, p, jobs = poised(registry, capture=None)
    await observer._rescue_jobs(p.id, screen, cursor)
    assert str(jobs.get("h1").state) == "running"


@pytest.mark.asyncio
async def test_a_participant_with_no_pane_cannot_be_rescued(registry):
    """No pane, no screen, no second opinion — so silence alone decides nothing."""
    observer, screen, cursor, p, jobs = poised(registry, pane=None)
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
    await observer._rescue_jobs(p.id, screen, cursor)
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
    observer._apply(p.id, batch, clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == "null"
    assert job.structured_result is None
    assert job.structured_status == "unavailable"


# ---- turn boundaries inside a batch ------------------------------------
#
# One poll drains everything written since the last one, so a batch is not a
# turn. These are the cases that used to leave a caller waiting for the 60s
# rescue, which is what "the conversation always dies on the second reply"
# actually was.


def spoke(text: str) -> Event:
    return Event(kind=EventKind.USER, text=text, turn_end=False)


def test_turn_positional_constructor_keeps_heard_position():
    turn = Turn("answer", ("prompt",))
    assert turn.said == "answer"
    assert turn.heard == ("prompt",)
    assert turn.raw_said == ""


def test_a_turn_end_mid_batch_still_answers(registry):
    """The reply plus the next prompt arrive together. The reply still lands."""
    observer, _screen, clock, p, jobs = poised(registry)
    batch = Batch(events=[said("the answer", turn_end=True), spoke("and now this")])
    observer._apply(p.id, batch, clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == "the answer"


def test_multi_block_raw_assistant_text_becomes_structured_result(registry):
    observer, _screen, clock, p, jobs = poised(registry, response_format="json")
    batch = Batch(
        events=[
            Event(
                kind=EventKind.ASSISTANT,
                text='{"answer":',
                raw_text='{"answer":',
            ),
            Event(
                kind=EventKind.ASSISTANT,
                text='"clipped"}',
                raw_text='"raw"}',
            ),
            Event(kind=EventKind.ASSISTANT, turn_end=True),
        ]
    )
    observer._apply(p.id, batch, clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == '{"answer":\n\n"clipped"}'
    assert job.structured_result == '{"answer":\n\n"raw"}'
    assert job.structured_status == "parsed"


def test_two_turns_in_one_batch_answer_two_jobs_in_order(registry):
    """Two boundaries, two waiting callers, each gets its own turn's text."""
    observer, _screen, clock, p, jobs = poised(registry)
    time.sleep(0.002)  # created_at is a float clock; keep the order unambiguous
    jobs.create(handle="h2", caller_id="caller", target_id=p.id, kind="send")
    batch = Batch(
        events=[
            said("first", turn_end=True),
            spoke("next question"),
            said("second", turn_end=True),
        ]
    )
    observer._apply(p.id, batch, clock, TurnAccumulator())
    assert jobs.get("h1").result == "first"
    assert jobs.get("h2").result == "second"


def test_one_turn_answers_only_the_caller_that_waited_longest(registry):
    """A queued second caller keeps waiting for its own turn, not this one."""
    observer, _screen, clock, p, jobs = poised(registry)
    time.sleep(0.002)
    jobs.create(handle="h2", caller_id="other", target_id=p.id, kind="send")
    observer._apply(p.id, Batch(events=[said("for h1", turn_end=True)]), clock, TurnAccumulator())
    assert jobs.get("h1").result == "for h1"
    assert str(jobs.get("h2").state) == "running"


def test_structured_undelivered_job_is_unavailable(registry):
    observer, _screen, clock, p, jobs = poised(
        registry,
        prompt="the injected prompt",
        response_format="json",
    )
    batch = Batch(
        events=[
            spoke("someone else's prompt"),
            said('{"answer": 1}', turn_end=True),
            spoke("another outside prompt"),
            said('{"answer": 2}', turn_end=True),
        ]
    )
    observer._apply(p.id, batch, clock, TurnAccumulator())
    job = jobs.get("h1")
    assert str(job.state) == "crashed"
    assert job.error_code == "prompt_never_seen"
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
    await observer._rescue_jobs(p.id, screen, clock)
    assert str(jobs.get("h1").state) == "done"
    assert str(jobs.get("h2").state) == "done"


# ---- the screen-to-status mapping in `_check_idle_screen` -------------
#
# The transcript can only settle IDLE or WORKING. An approval modal shows
# up as silence in the transcript, so the participant settles IDLE and the
# old gate — which only checked when status was WORKING — was closed before
# it was ever reached. The rewrite runs for any non-DEAD status.


class ReadingObserver:
    """An observer whose `screen_reading` returns a canned `ScreenReading`.

    Implements `is_idle_screen` only to satisfy the ABC; the reducer never
    calls it when `screen_reading` is overridden.
    """

    has_transcript = True

    def __init__(self, reading: ScreenReading):
        self._reading = reading

    def is_idle_screen(self, capture: str) -> bool:
        return self._reading.kind is ScreenKind.PROMPT

    def screen_reading(self, capture: str) -> ScreenReading:
        return self._reading


def screen_checked(registry, *, reading: ScreenReading, status=Status.IDLE):
    """A participant and an observer whose screen returns the given reading.

    `_capture` is monkey-patched so no subprocess is spawned.
    """
    observer = Observer(registry, harnesses={})
    p = registry.register(harness="vibe", pane="%1", cwd="/tmp")
    registry.set_status(p.id, status)

    async def capture_pane(_pane):
        return "$ "

    observer._capture = capture_pane
    return observer, ReadingObserver(reading), p


@pytest.mark.asyncio
async def test_an_idle_participant_at_an_approval_screen_becomes_awaiting_input(
    registry,
):
    """The regression: the old gate only checked when status was WORKING.

    A finished turn settles IDLE, so the participant was never screen-checked
    and AWAITING_INPUT was unreachable in the common case.
    """
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.LOW),
    )
    await observer._check_idle_screen(p.id, screen)
    assert registry.get(p.id).status is Status.AWAITING_INPUT


@pytest.mark.asyncio
async def test_a_working_participant_at_a_prompt_becomes_idle(registry):
    """The turn ended without an observed boundary.

    Settling IDLE is correct, and it is not safe to leave it to the rescue
    timer: `_rescue_jobs` does not touch participant status, so the
    participant would read WORKING forever.
    """
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.PROMPT, ScreenConfidence.LOW),
        status=Status.WORKING,
    )
    await observer._check_idle_screen(p.id, screen)
    assert registry.get(p.id).status is Status.IDLE


@pytest.mark.asyncio
async def test_an_unknown_screen_reading_leaves_the_status_untouched(registry):
    """UNKNOWN says nothing the reducer can act on."""
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.UNKNOWN, ScreenConfidence.LOW),
        status=Status.WORKING,
    )
    await observer._check_idle_screen(p.id, screen)
    assert registry.get(p.id).status is Status.WORKING

    # And from IDLE, it stays IDLE.
    observer2, screen2, p2 = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.UNKNOWN, ScreenConfidence.LOW),
        status=Status.IDLE,
    )
    await observer2._check_idle_screen(p2.id, screen2)
    assert registry.get(p2.id).status is Status.IDLE


@pytest.mark.asyncio
async def test_a_dead_participant_is_never_screen_checked(registry):
    """DEAD is terminal; a capture-pane on a dead pane is wasted work."""
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.HIGH),
        status=Status.DEAD,
    )
    await observer._check_idle_screen(p.id, screen)
    assert registry.get(p.id).status is Status.DEAD


# ---- the screen check on the path where nothing has attached -----------
#
# A source that has not found its input yet reports `waiting`, and that path
# used to skip every timer. The screen check is the only status channel that
# needs no transcript, so skipping it left Claude — which writes no transcript
# until its first message — pinned at its spawn-time IDLE, showing no status
# in the régie while it sat on a trust dialog.


class WaitingSource(Source):
    """Never attaches. What a transcript that does not exist yet looks like."""

    def __init__(self) -> None:
        self.reads = 0

    async def read(self) -> Batch:
        self.reads += 1
        return Batch(waiting=True)

    async def aclose(self) -> None:
        return None


class WaitingHarness:
    """Carries an observer whose source waits forever and whose screen speaks."""

    binary = "waiting"

    def __init__(self, reading: ScreenReading):
        self.observer = ReadingObserver(reading)
        self.observer.open_source = lambda **_: WaitingSource()  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_a_source_that_never_attaches_still_gets_a_screen_check(registry):
    """The regression, at the loop rather than the arm.

    Nothing is ever read, so only the waiting path runs. The pane shows an
    approval, and the participant must reach AWAITING_INPUT anyway.
    """
    harness = WaitingHarness(ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.HIGH))
    observer = Observer(
        registry, {"waiting": harness}, poll=0.01, search=0.01, sync=0.01, awaiting=0.0
    )

    async def capture_pane(_pane):
        return "Do you want to proceed?"

    observer._capture = capture_pane
    observer.start()
    try:
        p = registry.register(harness="waiting", pane="%1", cwd="/tmp")
        assert await until(lambda: registry.get(p.id).status is Status.AWAITING_INPUT)
    finally:
        await observer.aclose()


@pytest.mark.asyncio
async def test_the_waiting_path_starts_neither_the_relocate_nor_the_rescue_clock(
    registry,
):
    """Only the screen arm runs there, and the other two must stay unstarted.

    A rescue over a source that has never read anything would finish a caller's
    job with an empty `last_text` — a wrong answer where a wait belongs.
    """
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.HIGH),
    )
    observer.awaiting = 0.0
    clock = QuietClock()

    await observer._screen_only(p.id, screen, clock)
    assert clock.screen_quiet_since is not None
    assert clock.quiet_since is None
    assert clock.rescue_since is None


@pytest.mark.asyncio
async def test_the_waiting_screen_check_honours_its_own_window(registry):
    """No check until a full `awaiting` window of not-attaching has passed."""
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.APPROVAL, ScreenConfidence.HIGH),
    )
    observer.awaiting = AWAITING_INPUT_TIMEOUT
    clock = QuietClock()

    # First pass only starts the clock.
    await observer._screen_only(p.id, screen, clock)
    assert registry.get(p.id).status is Status.IDLE

    # A window later, it fires.
    clock.screen_quiet_since = time.monotonic() - AWAITING_INPUT_TIMEOUT - 1
    await observer._screen_only(p.id, screen, clock)
    assert registry.get(p.id).status is Status.AWAITING_INPUT


# ---- a harness that brings its own source ------------------------------


class ScriptedSource(Source):
    """Hands back canned batches, then goes quiet. Records that it was closed."""

    def __init__(self, batches):
        self.batches = list(batches)
        self.closed = False

    async def read(self) -> Batch:
        return self.batches.pop(0) if self.batches else Batch()

    async def aclose(self) -> None:
        self.closed = True


class ScriptedObserver:
    """An observer whose output is not a file, so it opens its own source.

    This is the whole contract an adapter over a database or an event stream
    has to meet: hand back something that produces batches. Everything the
    reducer does with them is the same code the file-backed adapters use.
    """

    has_transcript = True

    def __init__(self, *batches):
        self.source = ScriptedSource(batches)

    def open_source(self, *, cwd, session_id=None, after=None):
        return self.source

    def is_idle_screen(self, capture: str) -> bool:
        return False


class SourceHarness:
    """The launch half, present only to carry the observer.

    The reducer resolves a harness name to `harness.observer`, so a fake that
    goes into the registry needs both halves — but this one does nothing else,
    which is exactly the shape of the split.
    """

    binary = "scripted"

    def __init__(self, *batches):
        self.observer = ScriptedObserver(*batches)


class RawOnlySource(Source):
    """A live source whose records the adapter does not understand."""

    def __init__(self):
        self.refreshes = 0

    async def read(self) -> Batch:
        return Batch(progressed=True)

    async def refresh(self) -> Batch:
        self.refreshes += 1
        return Batch()


class RawOnlyObserver(ReadingObserver):
    has_transcript = True

    def __init__(self, kind=ScreenKind.PROMPT):
        super().__init__(ScreenReading(kind, ScreenConfidence.HIGH))
        self.source = RawOnlySource()

    def open_source(self, *, cwd, session_id=None, after=None):
        return self.source


class RawOnlyHarness:
    binary = "raw-only"

    def __init__(self, kind=ScreenKind.PROMPT):
        self.observer = RawOnlyObserver(kind)


def said(text: str, *, turn_end: bool) -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, turn_end=turn_end)


async def watching(registry, harness):
    observer = Observer(registry, {"scripted": harness}, poll=0.01, search=0.01, sync=0.01)
    observer.start()
    return observer


async def test_events_from_a_plugins_own_source_reach_the_bus(registry):
    harness = SourceHarness(Batch(events=[said("done", turn_end=True)], progressed=True))
    observer = await watching(registry, harness)
    try:
        p = registry.register(harness="scripted", pane=None, cwd="/tmp")
        assert await until(lambda: "agent.assistant" in kinds(registry.store))
        assert await until(lambda: registry.get(p.id).status is Status.IDLE)
    finally:
        await observer.aclose()


async def test_a_source_that_reports_status_is_believed_over_the_events(registry):
    """The channel a mutable-store source needs.

    The event says the turn is still going, which would infer WORKING. The
    source says otherwise, because it can ask the harness directly rather
    than inferring from silence. The source wins.
    """
    harness = SourceHarness(
        Batch(
            events=[said("still going", turn_end=False)],
            progressed=True,
            status=Status.IDLE,
        )
    )
    observer = await watching(registry, harness)
    try:
        p = registry.register(harness="scripted", pane=None, cwd="/tmp")
        assert await until(lambda: registry.get(p.id).status is Status.IDLE)
    finally:
        await observer.aclose()


async def test_the_source_is_closed_when_the_watcher_stops(registry):
    harness = SourceHarness()
    observer = await watching(registry, harness)
    registry.register(harness="scripted", pane=None, cwd="/tmp")
    assert await until(lambda: observer._tasks != {})
    await observer.aclose()
    assert harness.observer.source.closed


async def test_an_incomplete_attachment_source_retires_instead_of_spinning(registry):
    """A broken plugin logs once and stops; retrying cannot add its handshake."""
    harness = SourceHarness(Batch(attached=Attachment("scripted://session")))
    observer = Observer(registry, {"scripted": harness})
    p = registry.register(harness="scripted", pane=None, cwd="/tmp")

    await asyncio.wait_for(observer._watch(p.id, "scripted"), timeout=0.2)

    assert harness.observer.source.closed


def test_consumed_input_counts_as_activity_even_with_no_events(registry):
    """Bookkeeping records move the file without parsing to anything.

    If that read as silence, the rescue timer would fire during real work and
    hand a caller a half-finished answer.
    """
    observer = Observer(registry, harnesses={})
    clock = QuietClock()
    turns = TurnAccumulator()
    assert observer._apply("nobody", Batch(progressed=True), clock, turns) is True
    assert observer._apply("nobody", Batch(), clock, turns) is False


@pytest.mark.parametrize(
    ("screen_kind", "expected"),
    [
        (ScreenKind.PROMPT, Status.IDLE),
        (ScreenKind.APPROVAL, Status.AWAITING_INPUT),
    ],
)
async def test_raw_only_progress_keeps_screen_status_live_without_relocating_or_rescuing(
    registry, screen_kind, expected
):
    harness = RawOnlyHarness(screen_kind)
    jobs = JobManager(registry.store)
    observer = Observer(
        registry,
        {"raw-only": harness},
        jobs=jobs,
        poll=0.005,
        search=0.01,
        sync=0.01,
        relocate=0.0,
        awaiting=0.05,
        rescue=0.0,
    )
    captures = 0

    async def capture_pane(_pane):
        nonlocal captures
        captures += 1
        return "rendered prompt"

    observer._capture = capture_pane
    p = registry.register(harness="raw-only", pane="%1", cwd="/tmp")
    registry.set_status(p.id, Status.WORKING)
    jobs.create(handle="raw-only-job", caller_id="caller", target_id=p.id, kind="send")
    observer.start()
    try:
        assert await until(lambda: registry.get(p.id).status is expected)
        # Several more raw-only polls happen before the screen arm is due
        # again. They must not erase an approval verdict in the meantime.
        await asyncio.sleep(0.02)
        assert registry.get(p.id).status is expected
        assert captures > 0
        assert harness.observer.source.refreshes == 0
        assert jobs.get("raw-only-job").state == JobState.RUNNING
    finally:
        await observer.aclose()


async def test_raw_only_progress_honours_the_screen_throttle(registry, monkeypatch):
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH),
        status=Status.WORKING,
    )
    observer.awaiting = 10.0
    captures = 0

    async def capture_pane(_pane):
        nonlocal captures
        captures += 1
        return "rendered prompt"

    observer._capture = capture_pane
    tick = [5.0]
    monkeypatch.setattr(observer_mod.time, "monotonic", lambda: tick[0])
    clock = QuietClock(screen_quiet_since=0.0)
    raw = Batch(progressed=True)

    await observer._on_progress(p.id, screen, raw, clock)
    tick[0] = 11.0
    await observer._on_progress(p.id, screen, raw, clock)
    tick[0] = 12.0
    await observer._on_progress(p.id, screen, raw, clock)

    assert captures == 1
    assert registry.get(p.id).status is Status.IDLE


def test_events_count_as_activity_even_if_the_source_forgets_to_say_so(registry):
    """A forgiving contract: nothing breaks if a plugin omits `progressed`."""
    observer = Observer(registry, harnesses={})
    p = registry.register(harness="scripted", pane=None, cwd="/tmp")
    clock = QuietClock()
    batch = Batch(events=[said("hello", turn_end=False)])
    assert observer._apply(p.id, batch, clock, TurnAccumulator()) is True
    assert clock.last_text == "hello"


def test_correlation_failure_grace_starts_at_the_error_and_at_each_job(registry, monkeypatch):
    p = registry.register(harness="scripted", pane=None, cwd="/tmp")
    manager = JobManager(registry.store)
    old = manager.create(handle="old", caller_id="caller", target_id=p.id, kind="send")
    new = manager.create(handle="new", caller_id="caller", target_id=p.id, kind="send")
    registry.store.conn.execute(
        update(jobs_table).where(jobs_table.c.handle == old.handle).values(created_at=0.0)
    )
    registry.store.conn.execute(
        update(jobs_table).where(jobs_table.c.handle == new.handle).values(created_at=130.0)
    )
    clock = [100.0]
    monkeypatch.setattr(observer_mod, "wall_now", lambda: clock[0])
    observer = Observer(registry, harnesses={}, jobs=manager)
    failed = Batch(
        waiting=True,
        error_code="transcript_correlation_failed",
        error="receipt missing",
    )

    observer._update_source_error(p.id, failed)
    clock[0] = 129.0
    observer._update_source_error(p.id, failed)
    assert manager.get("old").state == JobState.RUNNING

    clock[0] = 131.0
    observer._update_source_error(p.id, failed)
    assert manager.get("old").state == JobState.CRASHED
    assert manager.get("new").state == JobState.RUNNING

    clock[0] = 161.0
    observer._update_source_error(p.id, failed)
    assert manager.get("new").state == JobState.CRASHED


def test_a_clean_source_batch_resets_correlation_failure_grace(registry, monkeypatch):
    p = registry.register(harness="scripted", pane=None, cwd="/tmp")
    manager = JobManager(registry.store)
    job = manager.create(handle="job", caller_id="caller", target_id=p.id, kind="send")
    registry.store.conn.execute(
        update(jobs_table).where(jobs_table.c.handle == job.handle).values(created_at=0.0)
    )
    clock = [100.0]
    monkeypatch.setattr(observer_mod, "wall_now", lambda: clock[0])
    observer = Observer(registry, harnesses={}, jobs=manager)
    failed = Batch(waiting=True, error_code="transcript_correlation_failed")

    observer._update_source_error(p.id, failed)
    clock[0] = 125.0
    observer._update_source_error(p.id, Batch(waiting=True))
    observer._update_source_error(p.id, failed)
    clock[0] = 140.0
    observer._update_source_error(p.id, failed)

    assert manager.get("job").state == JobState.RUNNING


@pytest.mark.parametrize(
    "error_code",
    ["transcript_correlation_failed", "transcript_correlation_ambiguous"],
)
async def test_await_surfaces_the_actionable_correlation_failure_message(registry, error_code):
    p = registry.register(harness="scripted", pane=None, cwd="/tmp")
    manager = JobManager(registry.store)
    manager.create(handle="job", caller_id="caller", target_id=p.id, kind="send")
    manager.finish(
        "job",
        state=JobState.CRASHED,
        error_code=error_code,
    )

    rows = await methods_mod.METHODS["jobs.await"](
        SimpleNamespace(jobs=manager, store=registry.store),
        {"handles": ["job"], "max_wait": 0},
    )

    assert rows[0]["error_code"] == error_code
    assert "may still be alive" in rows[0]["error"]
    assert "do not retry" in rows[0]["error"]
