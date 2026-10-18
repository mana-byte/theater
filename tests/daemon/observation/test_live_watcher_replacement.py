"""Replacing or cancelling a live watch hands partial evidence to its successor."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.support.live_observation import (
    BlockingEnrichment,
    CountingBatchSource,
    bus_texts,
    outcome,
)
from tests.support.observation import (
    DURABLE_CHANNEL,
    LIVE_CHANNEL,
    Rig,
    ScriptedDurable,
)
from tests.support.waiting import until
from theater.daemon.observation.live import LiveObservationHub, LiveRegistration
from theater.harness.contracts.channels import ChannelDeclaration, ChannelKind
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
)
from theater.harness.source import Batch, Source
from theater.models import JobState


async def test_repeated_live_changes_wait_for_old_watch_cleanup():
    """One restart owns cancellation, cleanup, and latest-registration rebuild."""
    from theater.daemon.observer import Observer

    observer = object.__new__(Observer)
    observer._stopping = asyncio.Event()
    observer._restart_pending = set()
    observer._restarts = set()
    observer._tasks = {}
    observer._pending_evidence = {}
    observer.registry = SimpleNamespace(list=lambda: [SimpleNamespace(id="p1")])
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
    observer._start_watch = lambda pid, **_kwargs: starts.append(pid)

    observer._on_live_change("p1")
    await cleanup_started.wait()
    observer._on_live_change("p1")
    await asyncio.sleep(0)
    observer._reconcile()

    assert starts == []
    assert "p1" in observer._restart_pending

    release_cleanup.set()
    await asyncio.gather(*tuple(observer._restarts))
    assert starts == ["p1"]
    assert "p1" not in observer._restart_pending


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
