"""Live terminal evidence finishes its exact job and is retained and replayed losslessly."""

from __future__ import annotations

import asyncio
import logging

import pytest

from tests.support.live_observation import CountingBatchSource, evidence_row, outcome, touch_paths
from tests.support.observation import (
    LIVE_CHANNEL,
    FakeHarness,
    Rig,
    ScriptedDurable,
    make_watch_context,
)
from tests.support.waiting import until, wait_until
from theater.daemon.observation.live import LiveObservationHub, LiveRegistration
from theater.daemon.observation.service import _WatchIteration
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.harness.contracts.events import Event, EventKind, EventPath
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
)
from theater.harness.source import Attachment, Batch, Source
from theater.models import JobState, Status


class RelocatingEvidenceSource(Source):
    def __init__(self, evidence):
        self.evidence = evidence
        self.held = ()
        self.refreshed = 0
        self.delivered = 0

    async def read(self) -> Batch:
        return Batch()

    async def refresh(self) -> Batch:
        self.refreshed += 1
        self.held = (self.evidence,)
        return Batch(terminal_evidence=self.held)

    def pending_terminal_evidence(self) -> bool:
        return bool(self.held)

    def terminal_evidence_snapshot(self):
        return self.held

    def terminal_evidence_delivered(self) -> None:
        self.delivered += 1
        self.held = ()


class CheckpointEvidenceSource(Source):
    def __init__(self, batch: Batch):
        self.batch = batch
        self.pending = None
        self.held = ()
        self.delivered = 0
        self.acknowledged = 0
        self.discarded = 0

    async def read(self) -> Batch:
        self.pending = "cursor-1"
        self.held = self.batch.terminal_evidence
        return self.batch

    def pending_source_checkpoint(self) -> str | None:
        return self.pending

    def acknowledge_source_checkpoint(self) -> None:
        self.acknowledged += 1
        self.pending = None

    def pending_terminal_evidence(self) -> bool:
        return bool(self.held)

    def terminal_evidence_delivered(self) -> None:
        self.delivered += 1
        self.held = ()

    def discard_attachment(self) -> None:
        self.discarded += 1


def relocation_watch(registry, source, sink, *, awaiting=999.0):
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    hub = LiveObservationHub()
    observer = Observer(
        registry,
        {"fake": FakeHarness()},
        live_hub=hub,
        relocate=0.0,
        awaiting=awaiting,
        rescue=999.0,
        poll=0.0,
    )
    hub.register(
        LiveRegistration(
            participant_id="p1",
            live_source=source,
            channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
            backend_generation=7,
            native_session_id="session-1",
            evidence_sink=sink,
        )
    )
    clock = QuietClock(quiet_since=-1.0, screen_quiet_since=-1.0)
    context = make_watch_context(
        observer, "p1", observer.harnesses["fake"].observer, source, clock, TurnAccumulator()
    )
    return observer, context


async def test_relocated_evidence_routes_from_the_production_watch(registry):
    evidence = outcome("turn-1", "session-1")
    source = RelocatingEvidenceSource(evidence)
    routed = []

    async def sink(pid, *, backend_generation, outcome):
        routed.append((pid, backend_generation, outcome))

    observer, context = relocation_watch(registry, source, sink)
    assert await observer._watch_iteration(context)

    assert source.refreshed == 1
    assert routed == [("p1", 7, evidence)]
    assert source.delivered == 1
    assert source.terminal_evidence_snapshot() == ()


async def test_cancelling_after_relocation_retains_its_exact_evidence(registry, monkeypatch):
    evidence = outcome("turn-1", "session-1")
    source = RelocatingEvidenceSource(evidence)
    routed = []
    screen_due = asyncio.Event()

    async def sink(pid, *, backend_generation, outcome):
        routed.append((pid, backend_generation, outcome))

    async def block_screen(*_args, **_kwargs):
        screen_due.set()
        await asyncio.Future()

    observer, context = relocation_watch(registry, source, sink, awaiting=0.0)
    monkeypatch.setattr(observer._reducer, "_screen_status_due", block_screen)
    watch = asyncio.create_task(observer._watch_iteration(context))
    await screen_due.wait()
    watch.cancel()
    with pytest.raises(asyncio.CancelledError):
        await watch

    assert source.refreshed == 1
    assert routed == []
    assert len(observer._pending_evidence["p1"]) == 1
    assert source.terminal_evidence_snapshot() == (evidence,)

    assert await observer._flush_pending_evidence("p1")
    assert routed == [("p1", 7, evidence)]
    assert source.delivered == 1
    assert observer._pending_evidence == {}


async def test_all_outer_and_inner_evidence_routes_are_attempted_after_a_failure(registry):
    evidence = tuple(outcome(f"turn-{index}", "session-1") for index in range(3))
    source = CheckpointEvidenceSource(Batch())
    source.pending = "cursor-1"
    source.held = evidence
    attempted = []

    async def sink(_pid, *, backend_generation, outcome):
        assert backend_generation == 7
        attempted.append(outcome.native_turn_id)
        if len(attempted) == 1:
            raise RuntimeError("first route failed")

    observer, context = relocation_watch(registry, source, sink)
    iteration = _WatchIteration(
        next_poll=0.0,
        batch=Batch(terminal_evidence=(evidence[0],)),
        inner=[Batch(terminal_evidence=(item,)) for item in evidence[1:]],
        applied=True,
    )
    await observer._finish_iteration(context, iteration)

    assert attempted == ["turn-0", "turn-1", "turn-2"]
    assert len(observer._pending_evidence["p1"]) == 1
    assert source.delivered == 0
    assert registry.store.get_participant("p1").source_checkpoint is None

    assert await observer._flush_pending_evidence("p1")
    assert source.delivered == 1
    assert source.acknowledged == 0
    assert registry.store.get_participant("p1").source_checkpoint is None
    assert observer._persist_pending_source_checkpoint("p1", source)
    assert source.acknowledged == 1
    assert registry.store.get_participant("p1").source_checkpoint == "cursor-1"


async def test_rejected_attachment_routes_evidence_but_holds_its_checkpoint(registry):
    evidence = outcome("turn-1", "session-1")
    batch = Batch(
        attached=Attachment(location="/tmp/untrusted", correlation="heuristic"),
        terminal_evidence=(evidence,),
    )
    source = CheckpointEvidenceSource(batch)
    source.pending = "cursor-1"
    source.held = (evidence,)
    routed = []

    async def sink(_pid, *, backend_generation, outcome):
        routed.append((backend_generation, outcome))

    observer, context = relocation_watch(registry, source, sink)
    assert await observer._handle_rejected_attachment(context, batch)

    assert source.discarded == 1
    assert routed == [(7, evidence)]
    assert source.delivered == 1
    assert source.acknowledged == 0
    assert source.pending_source_checkpoint() == "cursor-1"
    assert registry.store.get_participant("p1").source_checkpoint is None


async def test_contract_failure_routes_evidence_but_holds_its_checkpoint(registry):
    evidence = outcome("turn-1", "session-1")
    source = CheckpointEvidenceSource(
        Batch(
            waiting=True,
            attached=Attachment(location="/tmp/invalid", correlation="exact"),
            terminal_evidence=(evidence,),
        )
    )
    routed = []

    async def sink(_pid, *, backend_generation, outcome):
        routed.append((backend_generation, outcome))

    observer, context = relocation_watch(registry, source, sink)
    assert not await observer._watch_iteration(context)

    assert source.discarded == 1
    assert routed == [(7, evidence)]
    assert source.delivered == 1
    assert source.acknowledged == 0
    assert source.pending_source_checkpoint() == "cursor-1"
    assert registry.store.get_participant("p1").source_checkpoint is None


async def test_terminal_evidence_finishes_exact_job_with_evidence_committed_first(rig: Rig):
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    assert turn is not None
    rig.jobs.evidence_gate[job.handle] = (
        "p1",
        rig.state.backend_generation,
        rig.session,
        turn,
    )

    rig.register_live()
    rig.state.batches.append(
        Batch(
            events=(
                Event(
                    kind=EventKind.ASSISTANT,
                    text="working",
                    turn_id=turn,
                    paths=(EventPath(path="src/x.py", mode="write"),),
                ),
            ),
            progressed=True,
            status=Status.WORKING,
            terminal_evidence=(outcome(turn, rig.session),),
        )
    )

    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)
    done = rig.job(job.handle)
    assert done.result == "the answer"
    # The gate inside RecordingJobs.finish asserted evidence-before-finish.
    assert rig.jobs.finishes == [job.handle]
    # Exact job-to-turn path attribution: the event's turn owned the job.
    assert rig.jobs.path_touches == [(job.handle, ("src/x.py",))]
    assert touch_paths(rig.store, job.handle) == ["src/x.py"]


async def test_duplicate_evidence_finishes_exactly_once(rig: Rig):
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    rig.register_live()
    evidence = outcome(turn, rig.session)
    rig.state.batches.append(Batch(progressed=True, terminal_evidence=(evidence,)))
    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)

    # A duplicate delivery (replay or a second notification) never finishes
    # twice and never rewrites terminal state.
    rig.state.batches.append(Batch(progressed=True, terminal_evidence=(evidence,)))
    await wait_until(lambda: not rig.state.batches, what="the duplicate batch to be consumed")

    assert rig.jobs.finishes == [job.handle]
    assert rig.job(job.handle).result == "the answer"


async def test_evidence_durable_before_visible_completion_and_reconciliation_finishes_once(
    rig: Rig,
):
    """The intentional crash window: evidence committed, job still running."""
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    generation = rig.state.backend_generation
    session = rig.session

    async def crashing_sink(participant_id, *, backend_generation, outcome):
        # Persist first, then "crash" before the finish becomes visible.
        assert rig.store.record_native_terminal_evidence(
            evidence_row(participant_id, backend_generation, session, turn)
        )
        raise RuntimeError("crash between evidence commit and job finish")

    rig.register_live(evidence_sink=crashing_sink)
    rig.state.batches.append(Batch(progressed=True, terminal_evidence=(outcome(turn, session),)))

    assert await until(
        lambda: (
            rig.store.get_native_terminal_evidence(
                participant_id="p1",
                backend_generation=generation,
                native_session_id=session,
                native_turn_id=turn,
            )
            is not None
        )
    )
    assert rig.job(job.handle).state == JobState.RUNNING

    # Restart reconciliation finishes exactly once.
    finished = rig.service.finish_jobs_from_pending_evidence(["p1"])
    assert [j.handle for j in finished] == [job.handle]
    assert rig.job(job.handle).state == JobState.DONE
    assert rig.job(job.handle).result == "the answer"
    again = rig.service.finish_jobs_from_pending_evidence(["p1"])
    assert again == []
    assert rig.jobs.finishes == [job.handle]


async def test_queued_jobs_never_receive_results_or_touches(rig: Rig):
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    queued = await rig.service.queue_followup("p1", caller_id="caller", prompt="second")
    # The queue reservation is the QUEUED phase; the job itself stays
    # without an accumulator until dispatch, so it can never be attributed
    # touches while it waits.
    assert rig.store.queued_control_operation_count("p1") == 1

    rig.register_live()
    rig.state.batches.append(
        Batch(
            events=(
                Event(
                    kind=EventKind.ASSISTANT,
                    text="working",
                    turn_id=turn,
                    paths=(EventPath(path="src/first.py", mode="write"),),
                ),
            ),
            progressed=True,
            terminal_evidence=(outcome(turn, rig.session),),
        )
    )

    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)
    assert {handle for handle, _ in rig.jobs.path_touches} == {job.handle}
    assert rig.jobs.finishes == [job.handle]
    assert touch_paths(rig.store, queued.handle) == []


class CheckpointDurable(ScriptedDurable):
    """A durable reader that stages one armed checkpoint and records acks."""

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order
        self._armed = False
        self._staged = False
        self._pending: str | None = None

    def arm(self) -> None:
        self._armed = True

    async def read(self) -> Batch:
        batch = await super().read()
        if self._armed and not self._staged:
            self._staged = True
            self._pending = "cursor-1"
        return batch

    def pending_source_checkpoint(self) -> str | None:
        return self._pending

    def acknowledge_source_checkpoint(self) -> None:
        self.order.append("ack")
        self._pending = None


async def test_evidence_routes_before_the_checkpoint_is_acknowledged(rig: Rig, monkeypatch):
    """Evidence sink first, source checkpoint acknowledgement second."""

    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    order: list[str] = []

    durable = CheckpointDurable(order)
    rig.durable = durable

    async def ordering_sink(participant_id, *, backend_generation, outcome):
        order.append("sink")
        await rig.service.record_terminal_evidence(
            participant_id, backend_generation=backend_generation, outcome=outcome
        )

    rig.register_live(evidence_sink=ordering_sink)
    durable.arm()
    rig.state.batches.append(
        Batch(progressed=True, terminal_evidence=(outcome(turn, rig.session),))
    )

    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)
    # The checkpoint armed on the evidence-bearing read was acknowledged only
    # after the sink durably routed the outcome.
    assert order == ["sink", "ack"]
    assert rig.jobs.finishes == [job.handle]


async def test_transient_sink_failure_replays_retained_evidence_once(rig: Rig):
    """A failed sink delivery retains the outcome and retries; one finish."""
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    calls: list[str] = []
    real_sink = rig.service.record_terminal_evidence

    async def flaky_sink(participant_id, *, backend_generation, outcome):
        calls.append(outcome.native_turn_id)
        if len(calls) == 1:
            raise RuntimeError("sink hiccup before the evidence store")
        return await real_sink(
            participant_id, backend_generation=backend_generation, outcome=outcome
        )

    rig.register_live(evidence_sink=flaky_sink)
    rig.state.batches.append(
        Batch(progressed=True, terminal_evidence=(outcome(turn, rig.session),))
    )

    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)
    await wait_until(lambda: len(calls) >= 2, what="the retained evidence to replay")

    # First delivery failed, the retained evidence replayed, and the job
    # finished exactly once from the single successful routing.
    assert len(calls) == 2
    assert rig.jobs.finishes == [job.handle]
    assert rig.job(job.handle).result == "the answer"


async def test_no_sink_retains_evidence_and_retries_every_poll(rig: Rig, caplog):
    """Missing registration sink is a processing failure, never a drop."""
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    rig.register_live(evidence_sink=None)
    rig.state.batches.append(
        Batch(progressed=True, terminal_evidence=(outcome(turn, rig.session),))
    )

    with caplog.at_level(logging.WARNING):
        await wait_until(
            lambda: caplog.text.count("no registered evidence sink") >= 2,
            what="the missing-sink warning to repeat across polls",
        )

    assert rig.job(job.handle).state == JobState.RUNNING
    # Retention, not log-and-drop: the held outcome is re-routed on every
    # poll until a sink exists, so the warning repeats instead of the
    # evidence disappearing after the first attempt.
    assert caplog.text.count("no registered evidence sink") >= 2


async def test_live_only_evidence_survives_a_missing_sink_until_one_registers(live_only, caplog):
    """The generic draining-source path is lossless without source replay."""
    rig = live_only
    job = await rig.service.send("p1", caller_id="caller", prompt="live only")
    turn = rig.runtime.state.native_turn_id
    session = rig.runtime.state.native_session_id

    # No sink: routing fails, but the drained outcome is retained by the
    # observer and retried — never logged-and-dropped.
    rig.register(None)
    rig.runtime.state.batches.append(
        Batch(progressed=True, terminal_evidence=(outcome(turn, session),))
    )
    assert await until(lambda: rig.retained_evidence_count() == 1)
    with caplog.at_level(logging.WARNING):
        await wait_until(
            lambda: caplog.text.count("no registered evidence sink") >= 2,
            what="the missing-sink warning to repeat across poll retries",
        )
    assert rig.store.get_job(job.handle).state == JobState.RUNNING
    # Retention is deduplicated by exact session/turn identity across the
    # poll retries, not appended per retry.
    assert rig.retained_evidence_count() == 1

    # A proper registration replaces the wiring; the retained outcome is
    # flushed through the real sink and finishes its exact job once.
    rig.register(rig.service.record_terminal_evidence)
    assert await until(lambda: rig.store.get_job(job.handle).state == JobState.DONE)
    assert rig.jobs.finishes == [job.handle]
    assert rig.retained_evidence_count() == 0
    assert rig.store.get_job(job.handle).result == "the answer"


async def test_live_only_transient_sink_failure_replays_retained_evidence_once(live_only):
    rig = live_only
    job = await rig.service.send("p1", caller_id="caller", prompt="live only")
    turn = rig.runtime.state.native_turn_id
    session = rig.runtime.state.native_session_id
    calls: list[str] = []
    real_sink = rig.service.record_terminal_evidence

    async def flaky_sink(participant_id, *, backend_generation, outcome):
        calls.append(outcome.native_turn_id)
        if len(calls) == 1:
            raise RuntimeError("sink hiccup before the evidence store")
        return await real_sink(
            participant_id, backend_generation=backend_generation, outcome=outcome
        )

    rig.register(flaky_sink)
    rig.runtime.state.batches.append(
        Batch(progressed=True, terminal_evidence=(outcome(turn, session),))
    )

    assert await until(lambda: rig.store.get_job(job.handle).state == JobState.DONE)
    await wait_until(lambda: len(calls) >= 2, what="the retained outcome to flush through the sink")

    # The first delivery failed; the retained outcome flushed on a later
    # poll and finished the job exactly once, deduplicated by identity.
    assert len(calls) == 2
    assert rig.jobs.finishes == [job.handle]
    assert rig.retained_evidence_count() == 0


async def test_live_only_partial_pending_evidence_stops_before_next_maximum_batch(
    store, registry, caplog
):
    """Pending evidence applies backpressure before another source read."""
    from theater.daemon.observer import Observer

    first = tuple(outcome(f"first-{index}", "session-1") for index in range(511))
    second = tuple(outcome(f"second-{index}", "session-1") for index in range(512))
    source = CountingBatchSource(
        Batch(terminal_evidence=first),
        Batch(terminal_evidence=second),
    )
    observer = Observer(
        registry,
        {"fake": FakeHarness(has_transcript=False)},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    observer.live.register(
        LiveRegistration(
            participant_id="p1",
            live_source=source,
            channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
            backend_generation=1,
            native_session_id="session-1",
            evidence_sink=None,
        )
    )
    observer.start()
    try:
        assert await until(lambda: len(observer._pending_evidence.get("p1", ())) == 511)
        with caplog.at_level(logging.WARNING):
            await wait_until(
                lambda: caplog.text.count("no registered evidence sink") >= 2,
                what="the missing-sink warning to repeat across flush retries",
            )
        assert source.reads == 1
        assert len(observer._pending_evidence["p1"]) == 511
    finally:
        await observer.aclose()


async def test_retired_watch_retries_evidence_without_reopening_a_source(registry):
    """Retirement drains retained evidence under its original generation, without new reads."""
    from theater.daemon.observer import Observer

    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    opened = []
    observer = Observer(
        registry,
        {"fake": FakeHarness()},
        source_factory=lambda *_args, **_kwargs: opened.append("p1"),
        sync=0.01,
    )
    source = CountingBatchSource()
    allow_delivery = False
    seen: list[int] = []

    async def sink(_pid, *, backend_generation, outcome):
        del outcome
        seen.append(backend_generation)
        if not allow_delivery:
            raise RuntimeError("temporarily unavailable")

    registration = LiveRegistration(
        participant_id="p1",
        live_source=source,
        channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
        backend_generation=7,
        native_session_id="session-1",
        evidence_sink=sink,
    )
    registry.mark_dead("p1")
    batch = Batch(terminal_evidence=(outcome("turn-1", "session-1"),))

    assert await observer._route_terminal_evidence("p1", source, batch, registration) is False
    assert tuple(observer._pending_evidence["p1"]) == ((7, "session-1", "turn-1"),)

    observer.start()
    try:
        assert await until(lambda: len(seen) >= 2)
        allow_delivery = True
        assert await until(lambda: "p1" not in observer._pending_evidence)
        assert set(seen) == {7}
        assert opened == []
        assert "p1" not in observer._tasks
    finally:
        await observer.aclose()


def test_ack_terminal_evidence_tolerates_default_and_raising_sources(caplog):
    """Evidence release is typed: default no-op, and a raising release is logged, not fatal."""
    from theater.daemon.observer import Observer

    observer = object.__new__(Observer)

    class _DefaultSource(Source):
        async def read(self) -> Batch:
            return Batch()

    class _RaisingDelivered(_DefaultSource):
        def terminal_evidence_delivered(self) -> None:
            raise RuntimeError("release failed")

    with caplog.at_level(logging.DEBUG, logger="theater.observer"):
        observer._ack_terminal_evidence(_DefaultSource())
        observer._ack_terminal_evidence(_RaisingDelivered())

    assert "releasing terminal evidence failed" in caplog.text
