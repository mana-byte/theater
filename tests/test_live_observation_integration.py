"""Live-observation integration: registration seam through exact completion.

Wave 3B wires the runtime's live channel into the observation service through
the generic ``observer.live`` seam — no plugin internals imported, no lifecycle
imports. One fake harness with a durable reader plus ``tests/rig/fake_runtime``
drives the whole chain: registration composes a first-class ``HybridSource``
into the watch loop, live terminal evidence routes through a real
``ControlService`` and finishes its exact mapped job, and heuristic finishing
is suppressed for live-wired participants.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.daemon.controls import ControlGates, ControlService
from theater.daemon.jobs import JobManager
from theater.daemon.observation.live import LiveObservationHub, LiveRegistration
from theater.daemon.persistence.repositories.native_evidence import (
    NativeTerminalEvidence,
)
from theater.daemon.persistence.store import Store
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.events import Event, EventKind, EventPath
from theater.harness.contracts.observation import HarnessObserver
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    NativeTurnOutcome,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeContext,
    SessionOpenMode,
)
from theater.harness.source import Batch, Source
from theater.models import JobState, Status, now

LIVE_CHANNEL = ChannelDeclaration(id="fake-live", kind=ChannelKind.LIVE)
DURABLE_CHANNEL = ChannelDeclaration(id="transcript", kind=ChannelKind.TRANSCRIPT)
UNSET = object()


async def until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


class ScriptedDurable(Source):
    """The durable reader handed over by the (monkeypatched) open hook."""

    def __init__(self) -> None:
        self.batches: list[Batch] = []
        self.closed = False

    async def read(self) -> Batch:
        return self.batches.pop(0) if self.batches else Batch()

    async def aclose(self) -> None:
        self.closed = True


class BlockingEnrichment(Source):
    """An enrichment that exposes the primary-read/enrichment-read boundary."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.closed = False

    async def read(self) -> Batch:
        self.entered.set()
        await asyncio.Future()

    async def aclose(self) -> None:
        self.closed = True


class FakeHarnessObserver(HarnessObserver):
    """A transcript observer whose durable source is injected by the rig."""

    def __init__(self, *, has_transcript: bool = True):
        self.has_transcript = has_transcript

    def primary_channel_declaration(self):
        return DURABLE_CHANNEL if self.has_transcript else None

    def is_idle_screen(self, capture: str) -> bool:
        return True


class FakeHarness:
    """Just the ``.observer`` half the observation service needs."""

    def __init__(self, *, has_transcript: bool = True):
        self.observer = FakeHarnessObserver(has_transcript=has_transcript)


class RecordingJobs(JobManager):
    """JobManager recording finishes, path attribution, and the evidence gate."""

    def __init__(self, store: Store):
        super().__init__(store)
        self.store = store
        self.finishes: list[str] = []
        self.path_touches: list[tuple[str, tuple[str, ...]]] = []
        #: job handle -> evidence key that must be committed when finish runs.
        self.evidence_gate: dict[str, tuple[str, int, str, str]] = {}

    def finish(self, handle: str, **kwargs):
        if handle in self.evidence_gate:
            participant_id, generation, session, turn = self.evidence_gate[handle]
            assert (
                self.store.get_native_terminal_evidence(
                    participant_id=participant_id,
                    backend_generation=generation,
                    native_session_id=session,
                    native_turn_id=turn,
                )
                is not None
            ), "job finish ran before the evidence commit"
        self.finishes.append(handle)
        return super().finish(handle, **kwargs)

    def observe_paths(self, handle: str, paths) -> None:
        self.path_touches.append((handle, tuple(p.path for p in paths)))
        super().observe_paths(handle, paths)


def minimal_gates() -> ControlGates:
    async def noop(*args, **kwargs) -> None:
        return None

    return ControlGates(
        authorize=lambda *args: None,
        require_absent=noop,
        check_absent=lambda participant_id: None,
        send_preflight=noop,
        legacy_copy_mode_check=noop,
        legacy_busy_check=noop,
        check_prompt=lambda prompt: None,
        check_settings=lambda model, effort: None,
        cwd_for=lambda participant_id: "/tmp",
        legacy_deliver=noop,
    )


def make_runtime(participant_id: str, *, generation: int = 1) -> FakeRuntime:
    state = FakeRuntimeState(participant_id=participant_id, backend_generation=generation)
    context = RuntimeContext(
        participant_id=participant_id,
        cwd=None,
        io=FakeRuntimeIO(state),
        backend_generation=generation,
        endpoint=f"unix:///tmp/{participant_id}.sock",
        config_path=Path(f"/tmp/{participant_id}-config.json"),
    )
    runtime = FakeRuntime(context)
    runtime.state.backend_alive = True
    return runtime


def outcome(turn: str, session: str) -> NativeTurnOutcome:
    return NativeTurnOutcome(
        native_session_id=session,
        native_turn_id=turn,
        terminal=NativeTurnTerminal.COMPLETED,
        result="the answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
    )


def evidence_row(pid: str, generation: int, session: str, turn: str) -> NativeTerminalEvidence:
    return NativeTerminalEvidence(
        participant_id=pid,
        backend_generation=generation,
        native_session_id=session,
        native_turn_id=turn,
        terminal=NativeTurnTerminal.COMPLETED,
        result="the answer",
        completeness=ResultCompleteness.COMPLETE,
        provenance=ResultProvenance.NATIVE_EVIDENCE,
        recorded_at=now(),
    )


class Rig:
    """One observer plus one control service over one shared store."""

    def __init__(self, store: Store, registry, monkeypatch, *, poll: float = 0.02):
        from theater.daemon import observer as observer_mod
        from theater.daemon.observer import Observer

        self.store = store
        self.registry = registry
        self.durable = ScriptedDurable()
        monkeypatch.setattr(
            observer_mod, "open_participant_source", lambda observer, **kwargs: self.durable
        )
        self.jobs = RecordingJobs(store)
        self.runtime = make_runtime("p1")
        self.observer = Observer(
            registry,
            {"fake": FakeHarness()},
            poll=poll,
            search=poll,
            sync=poll,
            jobs=self.jobs,
        )
        self.service = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=lambda pid: self.runtime if pid == "p1" else None,
            gates=minimal_gates(),
        )
        self.observer.start()

    async def open(self) -> None:
        await self.runtime.open_session(mode=SessionOpenMode.NEW)

    @property
    def state(self) -> FakeRuntimeState:
        return self.runtime.state

    @property
    def session(self) -> str:
        assert self.state.native_session_id is not None
        return self.state.native_session_id

    def register_live(self, *, evidence_sink=UNSET, native_session_id=None) -> None:
        self.observer.live.register(
            LiveRegistration(
                participant_id="p1",
                live_source=self.runtime.live_source(),
                channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
                backend_generation=self.state.backend_generation,
                native_session_id=native_session_id or self.session,
                evidence_sink=(
                    self.service.record_terminal_evidence
                    if evidence_sink is UNSET
                    else evidence_sink
                ),
                active_job_for_turn=self.service.active_job_for_native_turn,
            )
        )

    async def aclose(self) -> None:
        await self.observer.aclose()
        await self.runtime.aclose()

    def job(self, handle: str):
        return self.store.get_job(handle)

    async def send(self, prompt: str = "do it"):
        return await self.service.send("p1", caller_id="caller", prompt=prompt)

    async def warm_up(self) -> None:
        """Prove the durable watch is alive with one durable event."""
        self.durable.batches.append(Batch(events=(Event(kind=EventKind.USER, text="warmup"),)))
        assert await until(lambda: "agent.user" in bus_kinds(self.store))


@pytest.fixture
async def rig(store, registry, monkeypatch):
    r = Rig(store, registry, monkeypatch)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await r.open()
    try:
        yield r
    finally:
        await r.aclose()


def bus_kinds(store, prefix="agent.") -> list[str]:
    return [row["kind"] for row in store.bus_tail(limit=500) if row["kind"].startswith(prefix)]


def bus_texts(store) -> list[str]:
    return [
        text
        for row in store.bus_tail(limit=500)
        if isinstance((payload := row["payload"]), dict)
        and isinstance((text := payload.get("text")), str)
    ]


def touch_paths(store, handle: str) -> list[str]:
    from sqlalchemy import select

    from theater.daemon.schema import touch as touch_table

    return [
        row.path
        for row in store.conn.execute(
            select(touch_table).where(touch_table.c.job_handle == handle)
        ).fetchall()
    ]


# ---- registration composes hybrid into the watch -------------------------


async def test_live_data_reaches_the_bus_only_through_registration(rig: Rig):
    await rig.warm_up()

    # Live wiring arrives after the watch already runs; the registration
    # itself recomposes the watch around the hybrid source.
    rig.register_live()
    rig.state.batches.append(Batch(events=(Event(kind=EventKind.ASSISTANT, text="live delta"),)))

    assert await until(lambda: "agent.assistant" in bus_kinds(rig.store))


async def test_wake_interrupts_sleep_long_before_the_poll_interval(store, registry, monkeypatch):
    """A wake makes the watch read promptly; the poll interval is only a fallback."""
    rig = Rig(store, registry, monkeypatch, poll=1.0)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await rig.open()
    try:
        rig.register_live()
        rig.state.batches.append(Batch(events=(Event(kind=EventKind.ASSISTANT, text="woken"),)))
        started = time.monotonic()
        rig.observer.live.wake("p1")

        assert await until(lambda: "agent.assistant" in bus_kinds(store))
        assert time.monotonic() - started < 0.8  # far below the 1.0s poll interval

        # The polling fallback still applies: no wake, the batch still lands
        # within roughly one poll interval.
        rig.state.batches.append(Batch(events=(Event(kind=EventKind.USER, text="polled"),)))
        assert await until(lambda: "agent.user" in bus_kinds(store))
    finally:
        await rig.aclose()


async def test_unregister_returns_participant_to_durable_only(rig: Rig):
    await rig.warm_up()

    rig.register_live()
    rig.observer.live.unregister("p1")
    await until(lambda: rig.observer.live.registration_for("p1") is None)
    rig.durable.batches.append(
        Batch(events=(Event(kind=EventKind.ASSISTANT, text="durable only"),))
    )
    assert await until(lambda: "agent.assistant" in bus_kinds(rig.store))

    rig.state.batches.append(
        Batch(events=(Event(kind=EventKind.USER, text="orphaned live delta"),))
    )
    await asyncio.sleep(0.3)
    assert bus_kinds(rig.store).count("agent.user") == 1


# ---- exact completion through the control service -----------------------


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
    await asyncio.sleep(0.2)

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


# ---- heuristic completion is suppressed for live-wired participants -------


async def test_heuristic_finishing_is_suppressed_while_live_wired(rig: Rig):
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    rig.register_live()

    # Every heuristic path is a no-op while the live channel owns completion.
    rig.observer._finish(job.handle, "heuristic")
    rig.observer._answer_turn("p1", "heuristic answer")
    rig.observer._release_jobs("p1", "heuristic answer")
    rig.observer._finish_identity_lost_jobs("p1", "heuristic answer")
    await rig.observer._rescue_jobs("p1", rig.observer.harnesses["fake"].observer, None)
    await asyncio.sleep(0.1)

    assert rig.job(job.handle).state == JobState.RUNNING
    assert rig.jobs.finishes == []

    # Exact evidence still finishes it, exactly once.
    rig.state.batches.append(
        Batch(progressed=True, terminal_evidence=(outcome(turn, rig.session),))
    )
    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)
    assert rig.jobs.finishes == [job.handle]


async def test_heuristic_finishing_resumes_after_unregister(rig: Rig):
    await rig.warm_up()
    job = await rig.send()
    rig.register_live()
    rig.observer.live.unregister("p1")
    await until(lambda: rig.observer.live.registration_for("p1") is None)

    rig.observer._finish(job.handle, "durable heuristic")

    assert rig.job(job.handle).state == JobState.DONE
    assert rig.jobs.finishes == [job.handle]


async def test_old_live_watcher_cannot_finish_heuristically_after_unregister(rig: Rig):
    """A bound live batch keeps exact-evidence authority while its watcher is cancelled."""
    from theater.daemon.observer import QuietClock, TurnAccumulator

    await rig.warm_up()
    job = await rig.send()
    rig.register_live()
    registration = rig.observer.live.registration_for("p1")
    assert registration is not None
    rig.observer.live.unregister("p1")

    stale_batch = Batch(
        events=(
            Event(
                kind=EventKind.ASSISTANT,
                text="stale durable turn end",
                turn_end=True,
                turn_id="stale-turn",
            ),
        ),
        progressed=True,
    )
    rig.observer._apply_source_batch(
        "p1",
        ScriptedDurable(),
        stale_batch,
        QuietClock(),
        TurnAccumulator(),
        registration=registration,
    )

    assert rig.job(job.handle).state == JobState.RUNNING
    assert rig.jobs.finishes == []


# ---- live-only wiring ------------------------------------------------------


async def test_live_only_wiring_settles_status_without_a_durable_reader(
    store, registry, monkeypatch
):
    from theater.daemon.observer import Observer

    jobs = RecordingJobs(store)
    runtime = make_runtime("p1")
    observer = Observer(
        registry,
        {"fake": FakeHarness(has_transcript=False)},
        poll=0.02,
        search=0.02,
        sync=0.02,
        jobs=jobs,
    )
    service = ControlService(
        store=store,
        jobs=jobs,
        runtime_for=lambda pid: runtime if pid == "p1" else None,
        gates=minimal_gates(),
    )
    p = registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    observer.start()
    await runtime.open_session(mode=SessionOpenMode.NEW)
    try:
        job = await service.send("p1", caller_id="caller", prompt="live only")
        turn = runtime.state.native_turn_id
        session = runtime.state.native_session_id
        observer.live.register(
            LiveRegistration(
                participant_id="p1",
                live_source=runtime.live_source(),
                channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
                backend_generation=runtime.state.backend_generation,
                native_session_id=session,
                evidence_sink=service.record_terminal_evidence,
                active_job_for_turn=service.active_job_for_native_turn,
            )
        )
        runtime.state.batches.append(
            Batch(
                events=(Event(kind=EventKind.ASSISTANT, text="live"),),
                progressed=True,
                status=Status.WORKING,
                terminal_evidence=(outcome(turn, session),),
            )
        )
        assert await until(lambda: store.get_job(job.handle).state == JobState.DONE)
        assert await until(lambda: registry.get(p.id).status is Status.WORKING)
        assert jobs.finishes == [job.handle]
    finally:
        await observer.aclose()
        await runtime.aclose()


async def test_live_explicit_awaiting_input_status_survives_progress_handling(rig: Rig):
    await rig.warm_up()
    observed: list[Status] = []
    original_on_progress = rig.observer._reducer.on_progress

    async def record_status(pid, observer, batch, clock):
        observed.append(rig.registry.get(pid).status)
        await original_on_progress(pid, observer, batch, clock)

    rig.observer._reducer.on_progress = record_status
    rig.register_live()
    rig.state.batches.append(Batch(status=Status.AWAITING_INPUT, progressed=True))

    assert await until(lambda: len(observed) >= 1)
    assert observed[0] is Status.AWAITING_INPUT
    assert rig.registry.get("p1").status is Status.AWAITING_INPUT

    rig.state.batches.append(Batch(status=Status.WORKING, progressed=True))
    assert await until(lambda: len(observed) >= 2)
    assert observed[1] is Status.WORKING
    assert rig.registry.get("p1").status is Status.WORKING


# ---- seam validation --------------------------------------------------------


async def test_registration_without_sink_routes_evidence_to_a_warning(rig: Rig, caplog):
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    rig.register_live(evidence_sink=None)
    rig.state.batches.append(
        Batch(progressed=True, terminal_evidence=(outcome(turn, rig.session),))
    )

    await asyncio.sleep(0.2)
    assert rig.job(job.handle).state == JobState.RUNNING
    assert "no registered evidence sink" in caplog.text


async def test_live_channel_colliding_with_durable_fails_the_watch_closed(rig: Rig):
    """A live channel reusing the durable id never observes with bad authority."""
    await rig.warm_up()
    colliding = ChannelDeclaration(id="transcript", kind=ChannelKind.LIVE)
    rig.observer.live.register(
        LiveRegistration(
            participant_id="p1",
            live_source=rig.runtime.live_source(),
            channel=LiveChannelDeclaration(channel=colliding),
            backend_generation=rig.state.backend_generation,
        )
    )
    rig.state.batches.append(
        Batch(events=(Event(kind=EventKind.ASSISTANT, text="must never land"),))
    )

    await asyncio.sleep(0.3)
    assert bus_kinds(rig.store).count("agent.assistant") == 0


# ---- correction round 1: durability, retention, arrival-driven wake --------


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


class ArrivingSource(Source):
    """A live source whose data arrival fires the installed activity callback.

    ``deliver`` is the receive loop: bounded data lands and the arrival is
    announced through the callback the hub installed — no wake call from
    the test, no task per message.
    """

    def __init__(self) -> None:
        self._queue: list[Batch] = []
        self._activity = None
        self.arrivals = 0

    def set_activity_callback(self, callback) -> None:
        self._activity = callback

    def deliver(self, batch: Batch) -> None:
        self._queue.append(batch)
        self.arrivals += 1
        if self._activity is not None:
            self._activity()

    async def read(self) -> Batch:
        return self._queue.pop(0) if self._queue else Batch()


class CountingBatchSource(Source):
    """A draining live source that records whether backpressure stopped reads."""

    def __init__(self, *batches: Batch) -> None:
        self.batches = list(batches)
        self.reads = 0

    async def read(self) -> Batch:
        self.reads += 1
        return self.batches.pop(0) if self.batches else Batch()


async def test_evidence_routes_before_the_checkpoint_is_acknowledged(rig: Rig, monkeypatch):
    """Evidence sink first, source checkpoint acknowledgement second."""
    from theater.daemon import observer as observer_mod

    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    order: list[str] = []

    durable = CheckpointDurable(order)
    monkeypatch.setattr(observer_mod, "open_participant_source", lambda observer, **kwargs: durable)

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
    await asyncio.sleep(0.1)

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
        await asyncio.sleep(0.12)

    assert rig.job(job.handle).state == JobState.RUNNING
    # Retention, not log-and-drop: the held outcome is re-routed on every
    # poll until a sink exists, so the warning repeats instead of the
    # evidence disappearing after the first attempt.
    assert caplog.text.count("no registered evidence sink") >= 2


async def test_hub_installs_replaces_and_detaches_activity_callbacks():
    hub = LiveObservationHub()
    first = ArrivingSource()
    second = ArrivingSource()

    def registration(source: ArrivingSource, generation: int) -> LiveRegistration:
        return LiveRegistration(
            participant_id="p1",
            live_source=source,
            channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
            backend_generation=generation,
        )

    hub.register(registration(first, 1))
    assert first._activity is not None
    assert second._activity is None

    # The installed callback is the wake hook: an arrival ends the poll sleep.
    first._activity()
    signal = hub.wake_signal("p1")
    assert signal is not None
    assert signal.is_set()

    # Replacement detaches the old source's callback before installing the new.
    hub.register(registration(second, 2))
    assert first._activity is None
    assert second._activity is not None

    hub.unregister("p1")
    assert second._activity is None


async def test_repeated_live_changes_wait_for_old_watch_cleanup():
    """One restart owns cancellation, cleanup, and latest-registration rebuild."""
    from theater.daemon.observer import Observer

    observer = object.__new__(Observer)
    observer._stopping = asyncio.Event()
    observer._restart_pending = set()
    observer._restarts = set()
    observer._tasks = {}
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    starts: list[str] = []

    async def old_watch() -> None:
        try:
            await asyncio.Future()
        finally:
            cleanup_started.set()
            await release_cleanup.wait()

    old_task = asyncio.create_task(old_watch())
    await asyncio.sleep(0)
    observer._tasks["p1"] = old_task
    observer._start_watch = starts.append

    observer._on_live_change("p1")
    await cleanup_started.wait()
    observer._on_live_change("p1")
    await asyncio.sleep(0)

    assert starts == []
    assert "p1" in observer._restart_pending

    release_cleanup.set()
    await asyncio.gather(*tuple(observer._restarts))
    assert starts == ["p1"]
    assert "p1" not in observer._restart_pending


async def test_live_arrival_wakes_observation_before_the_poll_interval(
    store, registry, monkeypatch
):
    """Real arrival-driven wakeups: initial delivery beats the poll interval."""
    rig = Rig(store, registry, monkeypatch, poll=1.0)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await rig.open()
    try:
        arriving = ArrivingSource()
        rig.observer.live.register(
            LiveRegistration(
                participant_id="p1",
                live_source=arriving,
                channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
                backend_generation=rig.state.backend_generation,
                native_session_id=rig.session,
                evidence_sink=rig.service.record_terminal_evidence,
                active_job_for_turn=rig.service.active_job_for_native_turn,
            )
        )
        # Let the recomposed watch settle so the wake measured below is the
        # arrival callback's, not registration's own.
        await asyncio.sleep(0.2)
        assert arriving._activity is not None

        started = time.monotonic()
        arriving.deliver(
            Batch(
                events=(Event(kind=EventKind.USER, text="arrived"),),
                progressed=True,
            )
        )

        assert await until(lambda: "agent.user" in bus_kinds(store), timeout=0.8)
        assert time.monotonic() - started < 0.8  # far below the 1.0s poll interval

        rig.observer.live.unregister("p1")
        assert await until(lambda: arriving._activity is None)
    finally:
        await rig.aclose()


# ---- correction round 2: lossless routing for the generic live-only path ----


class LiveOnlyRig:
    """A live-only observer (no durable reader) over one store and control service."""

    def __init__(self, store, registry, *, poll: float = 0.02):
        from theater.daemon.observer import Observer

        self.store = store
        self.jobs = RecordingJobs(store)
        self.runtime = make_runtime("p1")
        self.observer = Observer(
            registry,
            {"fake": FakeHarness(has_transcript=False)},
            poll=poll,
            search=poll,
            sync=poll,
            jobs=self.jobs,
        )
        self.service = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=lambda pid: self.runtime if pid == "p1" else None,
            gates=minimal_gates(),
        )
        self.observer.start()

    def register(self, evidence_sink) -> None:
        self.observer.live.register(
            LiveRegistration(
                participant_id="p1",
                live_source=self.runtime.live_source(),
                channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
                backend_generation=self.runtime.state.backend_generation,
                native_session_id=self.runtime.state.native_session_id,
                evidence_sink=evidence_sink,
                active_job_for_turn=self.service.active_job_for_native_turn,
            )
        )

    def retained_evidence_count(self) -> int:
        return len(self.observer._pending_evidence.get("p1", ()))


@pytest.fixture
async def live_only(store, registry):
    rig = LiveOnlyRig(store, registry)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await rig.runtime.open_session(mode=SessionOpenMode.NEW)
    try:
        yield rig
    finally:
        await rig.observer.aclose()
        await rig.runtime.aclose()


async def test_live_only_evidence_survives_a_missing_sink_until_one_registers(live_only):
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
    await asyncio.sleep(0.12)
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
    await asyncio.sleep(0.1)

    # The first delivery failed; the retained outcome flushed on a later
    # poll and finished the job exactly once, deduplicated by identity.
    assert len(calls) == 2
    assert rig.jobs.finishes == [job.handle]
    assert rig.retained_evidence_count() == 0


async def test_actual_watcher_replacement_preserves_partially_delivered_batch(rig: Rig):
    """Cancellation hands the whole batch to bounded observer-owned retention."""
    await rig.warm_up()
    job = await rig.send()
    mapped_turn = rig.state.native_turn_id
    assert mapped_turn is not None
    first = outcome("unmapped-turn", rig.session)
    second = outcome(mapped_turn, rig.session)
    second_entered = asyncio.Event()
    never_release = asyncio.Event()
    real_sink = rig.service.record_terminal_evidence

    async def partial_sink(participant_id, *, backend_generation, outcome):
        if outcome.native_turn_id == first.native_turn_id:
            return await real_sink(
                participant_id,
                backend_generation=backend_generation,
                outcome=outcome,
            )
        second_entered.set()
        await never_release.wait()
        return None

    durable_watch = rig.observer._tasks["p1"]
    rig.register_live(evidence_sink=partial_sink)
    assert await until(
        lambda: durable_watch.done() and rig.observer._tasks.get("p1") is not durable_watch
    )
    old_watch = rig.observer._tasks["p1"]
    rig.state.batches.append(Batch(terminal_evidence=(first, second)))
    assert await until(second_entered.is_set)

    # Replacing the registration cancels the watcher while its second sink
    # call awaits. The first outcome has already persisted; replaying the
    # complete batch is safe because the evidence store is first-write-wins.
    rig.register_live(evidence_sink=real_sink)

    assert await until(old_watch.done)
    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)
    assert (
        rig.store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=rig.state.backend_generation,
            native_session_id=rig.session,
            native_turn_id=first.native_turn_id,
        )
        is not None
    )
    assert (
        rig.store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=rig.state.backend_generation,
            native_session_id=rig.session,
            native_turn_id=second.native_turn_id,
        )
        is not None
    )
    assert rig.jobs.finishes == [job.handle]
    assert len(rig.observer._pending_evidence.get("p1", ())) == 0


async def test_actual_watcher_replacement_preserves_evidence_before_routing(rig: Rig):
    """Cancellation after apply but before routing transfers the drained outcome."""
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    assert turn is not None
    entered_progress = asyncio.Event()
    never_release = asyncio.Event()

    async def blocked_progress(*_args, **_kwargs):
        entered_progress.set()
        await never_release.wait()

    rig.observer._reducer.on_progress = blocked_progress
    durable_watch = rig.observer._tasks["p1"]
    rig.register_live()
    assert await until(
        lambda: durable_watch.done() and rig.observer._tasks.get("p1") is not durable_watch
    )
    old_watch = rig.observer._tasks["p1"]
    rig.state.batches.append(
        Batch(
            events=(Event(kind=EventKind.ASSISTANT, text="applied before routing"),),
            progressed=True,
            terminal_evidence=(outcome(turn, rig.session),),
        )
    )
    assert await until(entered_progress.is_set)

    # Rebinding cancels the old watch inside semantic progress handling,
    # before _route_terminal_evidence has been called for this batch.
    rig.register_live()

    assert await until(old_watch.done)
    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)
    assert rig.jobs.finishes == [job.handle]
    assert len(rig.observer._pending_evidence.get("p1", ())) == 0


async def test_actual_unregister_hands_held_evidence_to_later_registration(rig: Rig):
    """A missing sink cannot strand evidence inside the source being torn down."""
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    assert turn is not None

    durable_watch = rig.observer._tasks["p1"]
    rig.register_live(evidence_sink=None)
    assert await until(
        lambda: durable_watch.done() and rig.observer._tasks.get("p1") is not durable_watch
    )
    old_watch = rig.observer._tasks["p1"]
    rig.state.batches.append(Batch(terminal_evidence=(outcome(turn, rig.session),)))
    assert await until(lambda: len(rig.observer._pending_evidence.get("p1", ())) == 1)

    rig.observer.live.unregister("p1")
    assert await until(old_watch.done)
    assert rig.job(job.handle).state == JobState.RUNNING

    rig.register_live(evidence_sink=rig.service.record_terminal_evidence)
    assert await until(lambda: rig.job(job.handle).state == JobState.DONE)
    assert rig.jobs.finishes == [job.handle]
    assert len(rig.observer._pending_evidence.get("p1", ())) == 0


async def test_composite_hybrid_cancellation_transfers_primary_evidence(rig: Rig):
    """A wrapper await cannot hide Hybrid-held evidence from watcher teardown."""
    from theater.harness.channels import CompositeSource, EnrichmentBinding
    from theater.harness.channels.hybrid import HybridSource

    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    assert turn is not None
    blocker = BlockingEnrichment()
    binding = EnrichmentBinding(
        source=blocker,
        declaration=ChannelDeclaration(id="blocking-hook", kind=ChannelKind.HOOK),
    )
    rig.observer.hook_runtime = SimpleNamespace(
        has_active=lambda *_: True,
        enrichment_bindings=lambda *_: (binding,),
    )
    opened: list[Source] = []
    real_open = rig.observer._open_source_for_registration

    def capture_source(pid, observer, registration):
        source = real_open(pid, observer, registration)
        if source is not None:
            opened.append(source)
        return source

    rig.observer._open_source_for_registration = capture_source
    rig.state.batches.append(Batch(terminal_evidence=(outcome(turn, rig.session),)))
    rig.register_live()
    assert await until(blocker.entered.is_set)

    old_watch = rig.observer._tasks["p1"]
    old_source = opened[-1]
    assert isinstance(old_source, CompositeSource)
    assert isinstance(old_source._primary, HybridSource)
    assert old_source.pending_terminal_evidence() is True
    assert old_source.terminal_evidence_snapshot() == (outcome(turn, rig.session),)
    assert rig.state.batches == []

    # Replace the whole composition while the enrichment awaits. The new
    # watcher must consume the transferred outcome, not reread old wiring.
    rig.observer.hook_runtime = None
    rig.register_live()
    rig.state.batches.append(
        Batch(events=(Event(kind=EventKind.ASSISTANT, text="replacement marker"),))
    )
    assert await until(old_watch.done)
    assert await until(lambda: "replacement marker" in bus_texts(rig.store))

    assert rig.job(job.handle).state == JobState.DONE
    assert (
        rig.store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=rig.state.backend_generation,
            native_session_id=rig.session,
            native_turn_id=turn,
        )
        is not None
    )
    assert len(rig.observer._pending_evidence.get("p1", ())) == 0
    assert old_source.terminal_evidence_snapshot() == ()
    assert old_source._closed is True
    assert old_source._primary._closed is True
    assert blocker.closed is True


async def test_composite_live_only_cancellation_transfers_staged_primary_evidence(live_only):
    """Composite owns evidence from a draining live-only primary before enrichment awaits."""
    from theater.harness.channels import CompositeSource, EnrichmentBinding

    rig = live_only
    job = await rig.service.send("p1", caller_id="caller", prompt="live only")
    turn = rig.runtime.state.native_turn_id
    session = rig.runtime.state.native_session_id
    assert turn is not None and session is not None
    blocker = BlockingEnrichment()
    binding = EnrichmentBinding(
        source=blocker,
        declaration=ChannelDeclaration(id="blocking-hook", kind=ChannelKind.HOOK),
    )
    rig.observer.hook_runtime = SimpleNamespace(
        has_active=lambda *_: True,
        enrichment_bindings=lambda *_: (binding,),
    )
    opened: list[Source] = []
    real_open = rig.observer._open_source_for_registration

    def capture_source(pid, observer, registration):
        source = real_open(pid, observer, registration)
        if source is not None:
            opened.append(source)
        return source

    rig.observer._open_source_for_registration = capture_source
    rig.runtime.state.batches.append(Batch(terminal_evidence=(outcome(turn, session),)))
    rig.register(rig.service.record_terminal_evidence)
    assert await until(blocker.entered.is_set)

    old_watch = rig.observer._tasks["p1"]
    old_source = opened[-1]
    assert isinstance(old_source, CompositeSource)
    assert old_source.pending_terminal_evidence() is True
    assert old_source.terminal_evidence_snapshot() == (outcome(turn, session),)
    assert rig.runtime.state.batches == []

    rig.observer.hook_runtime = None
    rig.register(rig.service.record_terminal_evidence)
    rig.runtime.state.batches.append(
        Batch(events=(Event(kind=EventKind.ASSISTANT, text="live-only replacement"),))
    )
    assert await until(old_watch.done)
    assert await until(lambda: rig.runtime.state.batches == [])

    assert rig.store.get_job(job.handle).state == JobState.DONE
    assert (
        rig.store.get_native_terminal_evidence(
            participant_id="p1",
            backend_generation=rig.runtime.state.backend_generation,
            native_session_id=session,
            native_turn_id=turn,
        )
        is not None
    )
    assert len(rig.observer._pending_evidence.get("p1", ())) == 0
    assert old_source.terminal_evidence_snapshot() == ()
    assert old_source._closed is True
    assert blocker.closed is True


async def test_cancelled_route_transfers_maximum_batch_with_bound_generation():
    """Cancellation retention is bounded and keeps the source generation."""
    from theater.daemon.observer import Observer

    observer = object.__new__(Observer)
    observer.live = LiveObservationHub()
    observer._pending_evidence = {}
    source = CountingBatchSource()
    entered = asyncio.Event()
    never_release = asyncio.Event()
    outcomes = tuple(outcome(f"turn-{index}", "session-1") for index in range(512))

    async def blocked_sink(_pid, *, backend_generation, outcome):
        del backend_generation, outcome
        entered.set()
        await never_release.wait()

    registration = LiveRegistration(
        participant_id="p1",
        live_source=source,
        channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
        backend_generation=9,
        native_session_id="session-1",
        evidence_sink=blocked_sink,
    )
    batch = Batch(terminal_evidence=outcomes)
    routing = asyncio.create_task(
        observer._route_terminal_evidence("p1", source, batch, registration)
    )
    await entered.wait()
    routing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await routing

    pending = observer._pending_evidence["p1"]
    assert len(pending) == 512
    assert {key[0] for key in pending} == {9}


async def test_live_only_partial_pending_evidence_stops_before_next_maximum_batch(store, registry):
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
        await asyncio.sleep(0.08)
        assert source.reads == 1
        assert len(observer._pending_evidence["p1"]) == 511
    finally:
        await observer.aclose()


async def test_live_only_unregistered_evidence_keeps_bound_generation_for_retry():
    """Unregistering after a read never relabels retained evidence as generation zero."""
    from theater.daemon.observer import Observer

    observer = object.__new__(Observer)
    observer.live = LiveObservationHub()
    observer._pending_evidence = {}
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
    observer.live.register(registration)
    observer.live.unregister("p1")
    batch = Batch(terminal_evidence=(outcome("turn-1", "session-1"),))

    assert await observer._route_terminal_evidence("p1", source, batch, registration) is False
    assert tuple(observer._pending_evidence["p1"]) == ((7, "session-1", "turn-1"),)

    allow_delivery = True
    assert await observer._flush_pending_evidence("p1") is True
    assert seen == [7, 7]
    assert "p1" not in observer._pending_evidence


async def test_hybrid_replacement_routes_and_attributes_with_bound_registration():
    """A replacement cannot relabel old-source evidence or path ownership."""
    from theater.daemon.observer import Observer
    from theater.harness.channels.hybrid import HybridSource

    observer = object.__new__(Observer)
    observer.live = LiveObservationHub()
    observer._pending_evidence = {}
    old_live = CountingBatchSource(Batch(terminal_evidence=(outcome("turn-old", "session-old"),)))
    source = HybridSource(
        durable=ScriptedDurable(),
        live=old_live,
        live_channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
        durable_channel=DURABLE_CHANNEL,
    )
    routed: list[tuple[str, int]] = []
    lookups: list[tuple[str, int]] = []

    async def old_sink(_pid, *, backend_generation, outcome):
        routed.append((outcome.native_turn_id, backend_generation))

    async def new_sink(_pid, *, backend_generation, outcome):
        routed.append((f"new:{outcome.native_turn_id}", backend_generation))

    def old_lookup(_pid, *, backend_generation, native_session_id, native_turn_id):
        del native_session_id, native_turn_id
        lookups.append(("old", backend_generation))
        return SimpleNamespace(handle="old-job")

    def new_lookup(_pid, *, backend_generation, native_session_id, native_turn_id):
        del native_session_id, native_turn_id
        lookups.append(("new", backend_generation))
        return SimpleNamespace(handle="new-job")

    old = LiveRegistration(
        participant_id="p1",
        live_source=old_live,
        channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
        backend_generation=1,
        native_session_id="session-old",
        evidence_sink=old_sink,
        active_job_for_turn=old_lookup,
    )
    observer.live.register(old)
    batch = await source.read()
    observer.live.register(
        LiveRegistration(
            participant_id="p1",
            live_source=CountingBatchSource(),
            channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
            backend_generation=2,
            native_session_id="session-new",
            evidence_sink=new_sink,
            active_job_for_turn=new_lookup,
        )
    )

    assert await observer._route_terminal_evidence("p1", source, batch, old) is True
    event = Event(kind=EventKind.ASSISTANT, text="old", turn_id="turn-old")
    assert observer._path_target("p1", event, old) == "old-job"
    assert routed == [("turn-old", 1)]
    assert lookups == [("old", 1)]
