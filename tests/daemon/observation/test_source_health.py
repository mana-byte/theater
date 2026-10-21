"""A harness's own source: reading, cached health, and failure grace."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from tests.support.observation import apply_batch, bus_kinds
from tests.support.observer_rigs import ReadingObserver, said, screen_checked
from tests.support.waiting import until
from theater.daemon import methods as methods_mod
from theater.daemon.jobs import JobManager
from theater.daemon.observer import (
    Observer,
    QuietClock,
    TurnAccumulator,
)
from theater.daemon.schema import jobs as jobs_table
from theater.harness.channels.health import ChannelHealthTracker
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelHealth,
    ChannelHealthState,
    ChannelKind,
)
from theater.harness.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.source import Attachment, Batch, Source
from theater.models import (
    JobState,
    Status,
)


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


async def watching(registry, harness):
    observer = Observer(registry, {"scripted": harness}, poll=0.01, search=0.01, sync=0.01)
    observer.start()
    return observer


async def test_events_from_a_plugins_own_source_reach_the_bus(registry):
    harness = SourceHarness(Batch(events=[said("done", turn_end=True)], progressed=True))
    observer = await watching(registry, harness)
    try:
        p = registry.register(harness="scripted", pane=None, cwd="/tmp")
        assert await until(lambda: "agent.assistant" in bus_kinds(registry.store))
        assert await until(lambda: registry.get(p.id).status is Status.IDLE)
    finally:
        await observer.aclose()


async def test_source_backlog_skips_the_normal_poll_delay(registry):
    """A bounded source can drain its next batch after one loop yield."""
    harness = SourceHarness(
        Batch(progressed=True, has_more=True),
        Batch(events=[said("done", turn_end=True)], progressed=True),
    )
    observer = Observer(registry, {"scripted": harness}, poll=10, search=0.01, sync=0.01)
    observer.start()
    try:
        registry.register(harness="scripted", pane=None, cwd="/tmp")
        assert await until(lambda: "agent.assistant" in bus_kinds(registry.store), timeout=0.5)
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


async def test_source_construction_failure_uses_cannot_observe_diagnostic(registry, caplog):
    class BrokenObserver(ScriptedObserver):
        def open_source(self, *, cwd, session_id=None, after=None):
            raise ValueError("invalid participant observation context")

    harness = SourceHarness()
    harness.observer = BrokenObserver()
    observer = Observer(registry, {"scripted": harness})
    participant = registry.register(harness="scripted", pane=None, cwd="/tmp")
    caplog.set_level("WARNING", logger="theater.observer")

    await asyncio.wait_for(observer._watch(participant.id, "scripted"), timeout=0.2)

    assert "cannot observe" in caplog.text
    assert "invalid participant observation context" in caplog.text


async def test_raising_primary_read_updates_cached_health(registry):
    class RaisingSource(Source):
        async def read(self) -> Batch:
            raise ValueError("secret primary detail")

    class RaisingObserver(ScriptedObserver):
        def __init__(self) -> None:
            self.source = RaisingSource()

        def primary_channel_declaration(self) -> ChannelDeclaration:
            return ChannelDeclaration(id="primary", kind=ChannelKind.TRANSCRIPT)

    harness = SourceHarness()
    harness.observer = RaisingObserver()
    observer = await watching(registry, harness)
    participant = registry.register(harness="scripted", pane=None, cwd="/tmp")
    try:
        assert await until(
            lambda: (
                bool(observer.channel_health_snapshot(participant.id))
                and observer.channel_health_snapshot(participant.id)[0].state
                is ChannelHealthState.FAILED
            )
        )
        (health,) = observer.channel_health_snapshot(participant.id)
        assert health.diagnostics == ("primary read failed (ValueError)",)
        assert "secret primary detail" not in str(health)
    finally:
        await observer.aclose()
    assert observer.channel_health_snapshot(participant.id) == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["raises", "list", "items"])
async def test_untrusted_health_snapshot_does_not_stop_observation(registry, result):
    class UntrustedHealthSource(Source):
        def __init__(self) -> None:
            self.reads = 0
            self.closed = False

        async def read(self) -> Batch:
            self.reads += 1
            return Batch(progressed=True)

        def health_snapshot(self):
            if result == "raises":
                raise RuntimeError("secret health detail")
            if result == "list":
                return []
            return ("not-health",)

        async def aclose(self) -> None:
            self.closed = True

    harness = SourceHarness()
    source = UntrustedHealthSource()
    harness.observer.source = source
    observer = await watching(registry, harness)
    participant = registry.register(harness="scripted", pane=None, cwd="/tmp")
    try:
        assert await until(lambda: source.reads > 0)
        assert observer.channel_health_snapshot(participant.id) == ()
    finally:
        await observer.aclose()
    assert source.closed


@pytest.mark.asyncio
async def test_invalid_health_snapshot_clears_stale_cached_health(registry):
    class MutableHealthSource(Source):
        valid = True

        async def read(self) -> Batch:
            return Batch()

        def health_snapshot(self):
            if self.valid:
                return (ChannelHealth(channel_id="primary", state=ChannelHealthState.HEALTHY),)
            return ("not-health",)

    source = MutableHealthSource()
    observer = Observer(registry, harnesses={})
    observer._record_channel_health("participant", source)
    assert observer.channel_health_snapshot("participant")

    source.valid = False
    observer._record_channel_health("participant", source)

    assert observer.channel_health_snapshot("participant") == ()


@pytest.mark.asyncio
async def test_source_cancellation_does_not_report_primary_failure(registry):
    class CancelledSource(Source):
        async def read(self) -> Batch:
            raise asyncio.CancelledError

    observer = Observer(registry, harnesses={})
    tracker = ChannelHealthTracker("primary")
    tracker.mark_starting()
    observer._primary_channel_health[("participant", "primary")] = tracker

    with pytest.raises(asyncio.CancelledError):
        await observer._read_source("participant", CancelledSource())

    assert tracker.snapshot().state is ChannelHealthState.STARTING


def test_consumed_input_counts_as_activity_even_with_no_events(registry):
    """Bookkeeping records move the file without parsing to anything.

    If that read as silence, the rescue timer would fire during real work and
    hand a caller a half-finished answer.
    """
    observer = Observer(registry, harnesses={})
    clock = QuietClock()
    turns = TurnAccumulator()
    assert apply_batch(observer, "nobody", Batch(progressed=True), clock, turns) is True
    assert apply_batch(observer, "nobody", Batch(), clock, turns) is False


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
    p = registry.register(harness="raw-only", pane=None, cwd="/tmp")
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


async def test_raw_only_progress_honours_the_screen_throttle(registry):
    tick = [5.0]
    observer, screen, p = screen_checked(
        registry,
        reading=ScreenReading(ScreenKind.PROMPT, ScreenConfidence.HIGH),
        status=Status.WORKING,
        monotonic_clock=lambda: tick[0],
    )
    observer.awaiting = 10.0
    captures = 0

    async def capture_pane(_pane):
        nonlocal captures
        captures += 1
        return "rendered prompt"

    observer._capture = capture_pane
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
    assert apply_batch(observer, p.id, batch, clock, TurnAccumulator()) is True
    assert clock.last_text == "hello"


@pytest.mark.parametrize(
    "error_code", ["transcript_correlation_failed", "transcript_source_unavailable"]
)
def test_source_failure_grace_starts_at_the_error_and_at_each_job(
    registry, monkeypatch, error_code
):
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
    observer = Observer(registry, harnesses={}, jobs=manager, wall_clock=lambda: clock[0])
    failed = Batch(
        waiting=True,
        error_code=error_code,
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
    observer = Observer(registry, harnesses={}, jobs=manager, wall_clock=lambda: clock[0])
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
