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
import time
from pathlib import Path

import pytest

from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.daemon.controls import ControlGates, ControlService
from theater.daemon.jobs import JobManager
from theater.daemon.observation.live import LiveRegistration
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
        send_preflight=noop,
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
