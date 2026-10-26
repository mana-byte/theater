"""Live registration composes into the watch, wiring status and suppressing heuristic completion."""

from __future__ import annotations

import asyncio
import time

from tests.support.control_harness import make_runtime
from tests.support.live_observation import ArrivingSource, outcome
from tests.support.observation import (
    LIVE_CHANNEL,
    FakeHarness,
    RecordingJobs,
    Rig,
    ScriptedDurable,
    bus_kinds,
    make_watch_context,
    minimal_gates,
)
from tests.support.waiting import until, wait_until
from theater.daemon.controls import ControlService
from theater.daemon.observation.live import LiveObservationHub, LiveRegistration
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.observation import (
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
)
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    SessionOpenMode,
)
from theater.harness.contracts.source import NativeTurnState
from theater.harness.source import Attachment, Batch
from theater.models import JobState, Status


async def test_live_data_reaches_the_bus_only_through_registration(rig: Rig):
    await rig.warm_up()

    # Live wiring arrives after the watch already runs; the registration
    # itself recomposes the watch around the hybrid source.
    rig.register_live()
    rig.state.batches.append(Batch(events=(Event(kind=EventKind.ASSISTANT, text="live delta"),)))

    assert await until(lambda: "agent.assistant" in bus_kinds(rig.store))


async def test_wake_interrupts_sleep_long_before_the_poll_interval(store, registry, monkeypatch):
    """A wake makes the watch read promptly; the poll interval is only a fallback."""
    rig = Rig(store, registry, monkeypatch, poll=30.0)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await rig.open()
    try:
        rig.register_live()
        rig.state.batches.append(Batch(events=(Event(kind=EventKind.ASSISTANT, text="woken"),)))
        started = time.monotonic()
        rig.observer.live.wake("p1")

        assert await until(lambda: "agent.assistant" in bus_kinds(store))
        assert time.monotonic() - started < 5.0  # far below the 30.0s poll interval

        # The polling fallback still applies: no wake, the batch still lands
        # within roughly one poll interval.
        rig.state.batches.append(Batch(events=(Event(kind=EventKind.USER, text="polled"),)))
        assert await until(lambda: "agent.user" in bus_kinds(store), timeout=35.0)
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


async def test_bound_watch_keeps_exact_authority_through_error_attach_and_quiet_paths(
    store, registry, monkeypatch
):
    from theater.daemon.observer import Observer, QuietClock, TurnAccumulator

    class AttachingSource(ScriptedDurable):
        on_read = None

        async def read(self) -> Batch:
            batch = await super().read()
            if self.on_read is not None:
                self.on_read()
                self.on_read = None
            return batch

        def commit_attachment(self) -> None:
            return

    jobs = RecordingJobs(store)
    hub = LiveObservationHub()
    observer = Observer(
        registry,
        {"fake": FakeHarness()},
        live_hub=hub,
        jobs=jobs,
        failure_grace=0.0,
        poll=0.0,
        search=0.0,
        relocate=999.0,
        awaiting=999.0,
        rescue=999.0,
    )
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    jobs.create(handle="h1", caller_id="caller", target_id="p1", kind="send")
    source = AttachingSource()
    hub.register(
        LiveRegistration(
            participant_id="p1",
            live_source=source,
            channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
            backend_generation=1,
            native_session_id="session-1",
        )
    )
    context = make_watch_context(
        observer, "p1", observer.harnesses["fake"].observer, source, QuietClock(), TurnAccumulator()
    )
    source.on_read = lambda: hub.unregister("p1")

    source.batches.append(Batch(error_code="source_failed", error="broken read"))
    assert await observer._watch_iteration(context)
    assert hub.registration_for("p1") is None
    assert jobs.get("h1").state == JobState.RUNNING

    source.batches.append(
        Batch(
            attached=Attachment(
                location="/tmp/attached-transcript",
                session_id="session-1",
                correlation="exact",
                last_event=Event(kind=EventKind.ASSISTANT, text="stale answer", turn_end=True),
            ),
            progressed=True,
        )
    )
    assert await observer._watch_iteration(context)
    assert jobs.get("h1").state == JobState.RUNNING

    released = False

    async def release_unproven(*_args, **_kwargs):
        nonlocal released
        released = True

    monkeypatch.setattr(observer._completion, "release_unproven", release_unproven)
    source.batches.append(Batch(turn_state=NativeTurnState(turn_id="turn-1", complete=False)))
    assert await observer._watch_iteration(context)
    assert released is False

    rescued = False

    async def rescue_jobs(*_args, **_kwargs):
        nonlocal rescued
        rescued = True
        return False

    monkeypatch.setattr(observer._completion, "rescue_jobs", rescue_jobs)
    observer.rescue = 0.0
    context.clock.rescue_since = -1.0
    assert await observer._watch_iteration(context)
    assert rescued is False
    assert jobs.get("h1").state == JobState.RUNNING


def test_unbound_watch_checks_the_hub_each_time(store, registry):
    from theater.daemon.observer import Observer, QuietClock, TurnAccumulator

    jobs = RecordingJobs(store)
    hub = LiveObservationHub()
    observer = Observer(registry, {"fake": FakeHarness()}, live_hub=hub, jobs=jobs)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    jobs.create(handle="h1", caller_id="caller", target_id="p1", kind="send")
    source = ScriptedDurable()
    context = make_watch_context(
        observer, "p1", observer.harnesses["fake"].observer, source, QuietClock(), TurnAccumulator()
    )
    assert context.completion.registration is None

    hub.register(
        LiveRegistration(
            participant_id="p1",
            live_source=source,
            channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
            backend_generation=1,
        )
    )
    context.completion.answer_turn("p1", "suppressed")
    assert jobs.get("h1").state == JobState.RUNNING

    hub.unregister("p1")
    context.completion.answer_turn("p1", "allowed")
    assert jobs.get("h1").state == JobState.DONE


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


async def test_rested_status_restatement_does_not_walk_over_the_screen_verdict(rig: Rig):
    """A status-bearing batch with no progress is a restatement, not a settle.

    The live channel re-reports its unchanged reading every poll; if a
    restated status could settle, it would walk over the screen arm's
    awaiting verdict between polls and the two arms would flap
    awaiting/working forever.  Progress — new output — is what unblocks.
    """
    await rig.warm_up()
    rig.register_live()
    rig.state.batches.append(Batch(status=Status.AWAITING_INPUT, progressed=True))
    assert await until(lambda: rig.registry.get("p1").status is Status.AWAITING_INPUT)

    # The live channel restates its unchanged working reading: nothing
    # new happened, so the awaiting verdict must survive it.
    rig.state.batches.append(Batch(status=Status.WORKING, progressed=False))
    await wait_until(lambda: not rig.state.batches, what="the restated batch to be consumed")
    assert rig.registry.get("p1").status is Status.AWAITING_INPUT

    rig.state.batches.append(Batch(status=Status.WORKING, progressed=True))
    assert await until(lambda: rig.registry.get("p1").status is Status.WORKING)


async def test_native_awaiting_input_survives_the_screen_fallback(
    store,
    registry,
    monkeypatch,
) -> None:
    rig = Rig(store, registry, monkeypatch, awaiting=0.03)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await rig.open()

    async def capture(_pane: str) -> str:
        return "screen still renders the active turn"

    monkeypatch.setattr(rig.observer, "_capture", capture)
    harness_observer = rig.observer.harnesses["fake"].observer
    monkeypatch.setattr(
        harness_observer,
        "screen_reading",
        lambda _capture: ScreenReading(ScreenKind.WORKING, ScreenConfidence.HIGH),
    )
    try:
        await rig.warm_up()
        rig.register_live()
        rig.state.batches.append(Batch(status=Status.AWAITING_INPUT, progressed=True))
        assert await until(lambda: registry.get("p1").status is Status.AWAITING_INPUT)

        # Wait out the screen arm's quiet window (due at 0.03s) so the assert
        # proves the fallback read was suppressed, not merely unhurried.
        await wait_until(
            lambda: (rig.observer.quiet_seconds("p1") or 0.0) > 0.12,
            what="the screen arm's quiet window to elapse",
        )
        assert registry.get("p1").status is Status.AWAITING_INPUT

        rig.state.batches.append(Batch(status=Status.WORKING, progressed=True))
        assert await until(lambda: registry.get("p1").status is Status.WORKING)
    finally:
        await rig.aclose()


async def test_registration_without_sink_routes_evidence_to_a_warning(rig: Rig, caplog):
    await rig.warm_up()
    job = await rig.send()
    turn = rig.state.native_turn_id
    rig.register_live(evidence_sink=None)
    rig.state.batches.append(
        Batch(progressed=True, terminal_evidence=(outcome(turn, rig.session),))
    )

    await wait_until(
        lambda: "no registered evidence sink" in caplog.text,
        what="the missing-sink warning",
    )
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


async def test_live_arrival_wakes_observation_before_the_poll_interval(
    store, registry, monkeypatch
):
    """Real arrival-driven wakeups: initial delivery beats the poll interval."""
    rig = Rig(store, registry, monkeypatch, poll=30.0)
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
        # Let the recomposed watch consume the registration's own wake and
        # settle, so the wake measured below is the arrival callback's.
        await wait_until(
            lambda: not rig.observer.live.wake_signal("p1").is_set(),
            what="the recomposed watch to consume the registration wake",
        )
        assert arriving._activity is not None

        started = time.monotonic()
        arriving.deliver(
            Batch(
                events=(Event(kind=EventKind.USER, text="arrived"),),
                progressed=True,
            )
        )

        assert await until(lambda: "agent.user" in bus_kinds(store), timeout=5.0)
        assert time.monotonic() - started < 5.0  # far below the 30.0s poll interval

        rig.observer.live.unregister("p1")
        assert await until(lambda: arriving._activity is None)
    finally:
        await rig.aclose()


async def test_composed_durable_source_binds_the_live_wake_signal(store, registry, monkeypatch):
    rig = Rig(store, registry, monkeypatch)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await rig.open()
    try:
        bound: list[object] = []
        rig.durable.bind_wakeup = bound.append  # type: ignore[attr-defined]
        rig.register_live()
        signal = rig.observer.live.wake_signal("p1")
        assert signal is not None
        assert await until(lambda: any(w == signal.wake for w in bound))
    finally:
        await rig.aclose()


async def test_durable_only_watch_gets_a_wake_signal_and_releases_it(store, registry, monkeypatch):
    rig = Rig(store, registry, monkeypatch)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    try:
        bound: list[object] = []
        rig.durable.bind_wakeup = bound.append  # type: ignore[attr-defined]
        hub = rig.observer.live
        assert await until(lambda: bool(bound))
        assert hub._wakeups.keys() == ("p1",)
        assert any(w == hub.wake_signal("p1").wake for w in bound)
        assert hub.registration_for("p1") is None
    finally:
        await rig.aclose()
    assert hub._wakeups.keys() == ()


async def test_watch_close_keeps_a_live_registrations_signal(store, registry, monkeypatch):
    rig = Rig(store, registry, monkeypatch)
    registry.register(harness="fake", pane=None, cwd="/tmp", claimed_id="p1")
    await rig.open()
    try:
        rig.register_live()
        signal = rig.observer.live.wake_signal("p1")
        rig.observer.live.release_watch_signal("p1")
        assert rig.observer.live.wake_signal("p1") is signal
    finally:
        await rig.aclose()
