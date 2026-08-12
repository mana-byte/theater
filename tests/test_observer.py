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

import pytest

from theater.daemon.observer import (
    AWAITING_INPUT_TIMEOUT,
    RELOCATE_TIMEOUT,
    RESCUE_TIMEOUT,
    Observer,
    QuietClock,
)
from shipped import VibeHarness
from theater.harness.base import Event, EventKind
from theater.harness.source import Batch, Source
from theater.models import Status

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
    return [
        row["kind"]
        for row in store.bus_tail(limit=500)
        if row["kind"].startswith(prefix)
    ]


async def test_new_records_reach_the_bus_as_normalized_events(
    registry, vibe_tree, observing
):
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


async def test_attaching_skips_history_instead_of_replaying_it(
    registry, vibe_tree, observing
):
    """An adopted agent can have megabytes behind it. None of it is news."""
    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)
    registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    await asyncio.sleep(0.1)
    assert kinds(registry.store) == ["agent.transcript"]

    attach = [
        r for r in registry.store.bus_tail(limit=500) if r["kind"] == "agent.transcript"
    ][0]
    assert attach["payload"]["skipped_records"] == 4

    append(vibe_tree["transcript"], USER)
    assert await until(lambda: "agent.user" in kinds(registry.store))


async def test_attaching_derives_status_from_the_last_skipped_record(
    registry, vibe_tree, observing
):
    """A spawned agent that finished before we attached must not stay STARTING.

    The bus gets no history replayed, but the status must reflect the last
    record seen at attach time. This is the bug that left every spawned
    participant stuck at "starting" in the real run: the agent completed its
    turn in the 2 seconds it took the observer to find the transcript, no new
    bytes arrived after attach, and _drain never fired.
    """
    append(vibe_tree["transcript"], USER, WORKING, RESULT, DONE)
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    assert await until(lambda: registry.get(p.id).status is Status.IDLE)
    # The bus carries only the transcript attach event — no history replay.
    assert kinds(registry.store) == ["agent.transcript"]


async def test_attaching_to_a_working_agent_sets_working(
    registry, vibe_tree, observing
):
    """If the last record is a tool call (no turn_end), status is WORKING."""
    append(vibe_tree["transcript"], USER, WORKING)
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))

    assert await until(lambda: "agent.transcript" in kinds(registry.store))
    assert await until(lambda: registry.get(p.id).status is Status.WORKING)


async def test_the_harness_session_id_is_recorded_on_the_participant(
    registry, vibe_tree, observing
):
    p = registry.register(harness="vibe", pane=None, cwd=str(vibe_tree["project"]))
    assert await until(
        lambda: registry.get(p.id).session_id == "deadbeef-1111-2222-3333"
    )


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


async def test_a_truncated_transcript_is_re_read_from_the_top(
    registry, vibe_tree, observing
):
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


async def test_an_unobservable_participant_is_reported_once(
    registry, vibe_tree, observing, caplog
):
    """A misreported harness name must not become a silent blind spot."""
    caplog.set_level("WARNING", logger="theater.observer")
    registry.register(
        harness="claude_code", pane=None, cwd=str(vibe_tree["project"])
    )
    assert await until(lambda: "cannot observe" in caplog.text)
    await asyncio.sleep(0.1)
    assert caplog.text.count("cannot observe") == 1
    assert "known: vibe" in caplog.text


async def test_an_observer_with_no_transcript_waits_without_spinning_out(
    registry, tmp_path
):
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

    The relocate fires at 5s and the screen check at 10s. If the relocate
    reset the clock the screen check reads, 10s of silence never accumulates
    and AWAITING_INPUT is unreachable. Only the screen check may reset its
    own clock.
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


def test_the_rescue_clock_survives_the_screen_check_throttling_itself():
    """The rescue must not read a clock that another timer keeps pushing.

    `_check_idle_screen` throttles itself by setting screen_quiet_since to now
    every time it fires. A rescue reading that clock would restart every 10s
    and never reach its own, much longer, timeout — the same shape of bug as
    the relocate one above, but silent: the symptom is a caller that waits
    forever, not a status that never changes.
    """
    cursor = QuietClock()
    cursor.begin_quiet(0.0)

    # Six screen windows go by, each one throttling the screen clock.
    t = 0.0
    for _ in range(6):
        t += AWAITING_INPUT_TIMEOUT + 0.5
        assert cursor.screen_quiet_for(t) > AWAITING_INPUT_TIMEOUT
        cursor.screen_quiet_since = t

    # The rescue clock has been counting that whole minute regardless.
    assert cursor.rescue_quiet_for(t) == t
    assert cursor.rescue_quiet_for(t) > RESCUE_TIMEOUT


def test_new_bytes_restart_the_rescue_clock_too():
    cursor = QuietClock()
    cursor.begin_quiet(0.0)
    cursor.stir()
    assert cursor.rescue_since is None
    assert cursor.rescue_quiet_for(90.0) == 0.0


# ---- rescuing a job whose turn end was never read ---------------------


class ScreenHarness:
    """Just enough harness for `_rescue_jobs`, which reads one method."""

    has_transcript = True

    def __init__(self, idle: bool):
        self.idle = idle

    def is_idle_screen(self, capture: str) -> bool:
        return self.idle


def poised(registry, *, pane="%1", idle=True, capture="$ "):
    """A participant with one running job, and an observer poised to rescue it.

    `capture=None` stands for a pane that could not be read at all.
    """
    from theater.daemon.jobs import JobManager

    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane=pane, cwd="/tmp")
    jobs.create(handle="h1", caller_id="caller", target_id=p.id, kind="send")
    observer = Observer(registry, harnesses={}, jobs=jobs)

    async def capture_pane(_pane):
        return capture

    observer._capture = capture_pane
    cursor = QuietClock()
    cursor.last_text = "the last thing it said"
    return observer, ScreenHarness(idle), cursor, p, jobs


@pytest.mark.asyncio
async def test_a_quiet_participant_over_an_idle_screen_releases_its_caller(registry):
    observer, harness, cursor, p, jobs = poised(registry)
    await observer._rescue_jobs(p.id, harness, cursor)
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
    observer, harness, cursor, p, jobs = poised(registry, idle=False)
    await observer._rescue_jobs(p.id, harness, cursor)
    assert str(jobs.get("h1").state) == "running"


@pytest.mark.asyncio
async def test_an_unreadable_screen_decides_nothing(registry):
    observer, harness, cursor, p, jobs = poised(registry, capture=None)
    await observer._rescue_jobs(p.id, harness, cursor)
    assert str(jobs.get("h1").state) == "running"


@pytest.mark.asyncio
async def test_a_participant_with_no_pane_cannot_be_rescued(registry):
    """No pane, no screen, no second opinion — so silence alone decides nothing."""
    observer, harness, cursor, p, jobs = poised(registry, pane=None)
    await observer._rescue_jobs(p.id, harness, cursor)
    assert str(jobs.get("h1").state) == "running"


@pytest.mark.asyncio
async def test_a_turn_end_that_was_actually_read_carries_no_error_code(registry):
    observer, _harness, _cursor, p, jobs = poised(registry)
    observer._answer_turn(p.id, "a real reply")
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == "a real reply"
    assert job.error_code is None


# ---- turn boundaries inside a batch ------------------------------------
#
# One poll drains everything written since the last one, so a batch is not a
# turn. These are the cases that used to leave a caller waiting for the 60s
# rescue, which is what "the conversation always dies on the second reply"
# actually was.


def spoke(text: str) -> Event:
    return Event(kind=EventKind.USER, text=text, turn_end=False)


def test_a_turn_end_mid_batch_still_answers(registry):
    """The reply plus the next prompt arrive together. The reply still lands."""
    observer, _harness, clock, p, jobs = poised(registry)
    batch = Batch(events=[said("the answer", turn_end=True), spoke("and now this")])
    observer._apply(p.id, batch, clock)
    job = jobs.get("h1")
    assert str(job.state) == "done"
    assert job.result == "the answer"


def test_two_turns_in_one_batch_answer_two_jobs_in_order(registry):
    """Two boundaries, two waiting callers, each gets its own turn's text."""
    observer, _harness, clock, p, jobs = poised(registry)
    time.sleep(0.002)  # created_at is a float clock; keep the order unambiguous
    jobs.create(handle="h2", caller_id="caller", target_id=p.id, kind="send")
    batch = Batch(
        events=[
            said("first", turn_end=True),
            spoke("next question"),
            said("second", turn_end=True),
        ]
    )
    observer._apply(p.id, batch, clock)
    assert jobs.get("h1").result == "first"
    assert jobs.get("h2").result == "second"


def test_one_turn_answers_only_the_caller_that_waited_longest(registry):
    """A queued second caller keeps waiting for its own turn, not this one."""
    observer, _harness, clock, p, jobs = poised(registry)
    time.sleep(0.002)
    jobs.create(handle="h2", caller_id="other", target_id=p.id, kind="send")
    observer._apply(p.id, Batch(events=[said("for h1", turn_end=True)]), clock)
    assert jobs.get("h1").result == "for h1"
    assert str(jobs.get("h2").state) == "running"


def test_a_boundary_with_no_text_answers_with_the_turn(registry):
    """Codex ends a turn on `task_complete`, a record that carries no message."""
    observer, _harness, clock, p, jobs = poised(registry)
    batch = Batch(
        events=[
            said("what it actually said", turn_end=False),
            Event(kind=EventKind.ASSISTANT, text="", turn_end=True),
        ]
    )
    observer._apply(p.id, batch, clock)
    assert jobs.get("h1").result == "what it actually said"


@pytest.mark.asyncio
async def test_rescue_still_releases_every_waiting_caller(registry):
    """The backstop stays fan-out: nothing else will ever free the rest.

    Per-turn matching has already failed by the time rescue fires, so there is
    no boundary left to pair a job with. Releasing one at a time would drip the
    queue out over one rescue window each.
    """
    observer, harness, clock, p, jobs = poised(registry)
    jobs.create(handle="h2", caller_id="other", target_id=p.id, kind="send")
    await observer._rescue_jobs(p.id, harness, clock)
    assert str(jobs.get("h1").state) == "done"
    assert str(jobs.get("h2").state) == "done"


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


class SourceHarness:
    """A harness whose output is not a file, so it overrides `open_source`.

    This is the whole contract an adapter over a database or an event stream
    has to meet: hand back something that produces batches. Everything the
    observer does with them is the same code the file-backed harnesses use.
    """

    has_transcript = True
    binary = "scripted"

    def __init__(self, *batches):
        self.source = ScriptedSource(batches)

    def open_source(self, *, cwd, session_id=None, after=None):
        return self.source

    def is_idle_screen(self, capture: str) -> bool:
        return False


def said(text: str, *, turn_end: bool) -> Event:
    return Event(kind=EventKind.ASSISTANT, text=text, turn_end=turn_end)


async def watching(registry, harness):
    observer = Observer(
        registry, {"scripted": harness}, poll=0.01, search=0.01, sync=0.01
    )
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
    assert harness.source.closed


def test_consumed_input_counts_as_activity_even_with_no_events(registry):
    """Bookkeeping records move the file without parsing to anything.

    If that read as silence, the rescue timer would fire during real work and
    hand a caller a half-finished answer.
    """
    observer = Observer(registry, harnesses={})
    clock = QuietClock()
    assert observer._apply("nobody", Batch(progressed=True), clock) is True
    assert observer._apply("nobody", Batch(), clock) is False


def test_events_count_as_activity_even_if_the_source_forgets_to_say_so(registry):
    """A forgiving contract: nothing breaks if a plugin omits `progressed`."""
    observer = Observer(registry, harnesses={})
    p = registry.register(harness="scripted", pane=None, cwd="/tmp")
    clock = QuietClock()
    batch = Batch(events=[said("hello", turn_end=False)])
    assert observer._apply(p.id, batch, clock) is True
    assert clock.last_text == "hello"
