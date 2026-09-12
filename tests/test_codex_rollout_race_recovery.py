"""Rollout-race regression: a deferred thread/resume subscription recovers on idle.

Incident shape (job ce8a6e7c945d#269): the first ``send`` after UI session
creation can race the backend's rollout flush — ``thread/resume`` answers
"no rollout found" and the runtime used to swallow that silently, leaving the
connection unsubscribed forever. ``turn/completed`` is delivered only to
subscribed connections, so Theater saw the participant go idle but never saw
turn completion; the observation layer suppressed transcript completion and
the accepted job hung RUNNING until killed.

The fix is bounded by construction: the "no rollout found" branch records a
diagnostic (never silent), and the ``thread/status/changed`` idle broadcast —
which reaches every initialized connection after turn completion, when the
rollout certainly exists — schedules exactly one recovery attempt. A
successful ``thread/resume`` runs the normal reconcile path, which records
terminal outcomes for completed turns, so the same stuck job finishes via
routed native terminal evidence.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.test_codex_native_runtime_plugin import (
    GENERATION,
    PARTICIPANT,
    ScriptedCodexServer,
    open_new,
)
from tests.test_live_observation_integration import minimal_gates
from theater.daemon.awaiting import REASON_JOB_TERMINAL, coordinate_await, parse_targets
from theater.daemon.controls.service import ControlService
from theater.daemon.jobs import JobManager
from theater.daemon.observation.live import LiveObservationHub, LiveRegistration
from theater.daemon.observation.reducer import QuietClock
from theater.daemon.observation.service import Observer
from theater.daemon.observation.turns import TurnAccumulator
from theater.daemon.registry import Registry
from theater.harness.builtin.plugins.codex.manifest import MANIFEST
from theater.harness.channels.hybrid import HybridSource
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.runtime import RuntimeNotification, RuntimeRequestError
from theater.harness.contracts.source import Batch, Source
from theater.models import JobState, Participant, Tier


class CompletedTranscript(Source):
    """The durable half: the transcript already holds the finished turn."""

    async def read(self) -> Batch:
        return Batch(
            events=(Event(kind=EventKind.ASSISTANT, text="done", turn_id="turn-1", turn_end=True),),
            progressed=True,
        )


class Absent:
    """A presence provider that never protects the target."""

    revision = 1

    def snapshot(self, participant_id):
        from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState

        return PresenceSnapshot(PresenceState.ABSENT, "test", 1, None)

    async def refresh(self) -> None:
        return None

    async def wait_for_change(self, revision):
        await asyncio.Future()


def idle_broadcast(thread_id: str = "ui-thread-1") -> RuntimeNotification:
    return RuntimeNotification(
        method="thread/status/changed",
        params={"threadId": thread_id, "status": {"type": "idle"}},
    )


def completed_resume_response() -> dict:
    """thread/resume once the rollout exists: one already-completed turn."""
    return {
        "thread": {"id": "ui-thread-1", "status": {"type": "idle"}},
        "initialTurnsPage": {
            "data": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [{"id": "i-1", "type": "agentMessage", "text": "finished"}],
                }
            ]
        },
    }


async def until(predicate, timeout: float = 2.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return False


class RolloutRaceRig:
    """One codex runtime, control service, and observer over one shared store."""

    def __init__(self, store):
        self.server = ScriptedCodexServer()
        self.store = store
        self.runtime = None
        self.jobs = JobManager(store)
        self.controls = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=lambda _pid: self.runtime,
            gates=minimal_gates(),
        )
        self.observer = Observer(Registry(store), jobs=self.jobs, live_hub=LiveObservationHub())
        store.upsert_participant(
            Participant(
                id=PARTICIPANT,
                harness="codex",
                tier=Tier.SPAWNED,
                tmux_pane="%audit",
                cwd="/repo",
                parent_id="parent",
            )
        )

    async def open(self) -> None:
        self.runtime, _binding = await open_new(self.server)

    async def fail_resume_with_no_rollout(self) -> None:
        self.server.fail(
            "thread/resume",
            RuntimeRequestError(-32600, "no rollout found for thread id ui-thread-1"),
        )

    async def send(self):
        return await self.controls.send(PARTICIPANT, caller_id="parent", prompt="finish")

    def live_registration(self) -> tuple[HybridSource, LiveRegistration]:
        live = self.runtime.live_source()
        source = HybridSource(
            durable=CompletedTranscript(), live=live, live_channel=MANIFEST.runtime.channel
        )
        registration = LiveRegistration(
            participant_id=PARTICIPANT,
            live_source=live,
            channel=MANIFEST.runtime.channel,
            backend_generation=GENERATION,
            native_session_id="ui-thread-1",
            evidence_sink=self.controls.record_terminal_evidence,
            active_job_for_turn=self.controls.active_job_for_native_turn,
        )
        return source, registration

    async def aclose(self) -> None:
        await self.runtime.aclose()


async def test_no_rollout_first_send_is_diagnosed_not_swallowed(store) -> None:
    rig = RolloutRaceRig(store)
    await rig.open()
    try:
        await rig.fail_resume_with_no_rollout()
        job = await rig.send()
        # The turn was accepted: the job runs, but the subscription never landed.
        assert job.state == JobState.RUNNING.value
        assert rig.runtime._subscribed is False
        assert len(rig.server.requested("thread/resume")) == 1
        # The silent swallow is gone: the deferral is a recorded diagnostic.
        snapshot = await rig.runtime.snapshot()
        assert any("no rollout yet" in text for text in snapshot.health_diagnostics)
        assert any("thread/resume deferred" in text for text in snapshot.health_diagnostics)
        # Degrade is reserved for other errors: health stays connected.
        assert snapshot.health.value == "connected"
    finally:
        await rig.aclose()


async def test_idle_broadcast_recovers_subscription_and_finishes_same_job(store) -> None:
    rig = RolloutRaceRig(store)
    await rig.open()
    try:
        # Phase 1: the first send hits the first-turn rollout race.
        await rig.fail_resume_with_no_rollout()
        job = await rig.send()
        assert job.state == JobState.RUNNING.value
        assert rig.runtime._subscribed is False

        # The rollout exists once the turn completed; a successful resume
        # would now reconcile the completed turn as terminal evidence.
        rig.server.failures.pop("thread/resume")
        rig.server.respond("thread/resume", completed_resume_response())

        # Phase 2: repeated idle broadcasts must schedule exactly one
        # concurrent recovery attempt — gate the request to hold it in flight.
        gate = asyncio.Event()
        rig.server.request_gates["thread/resume"] = gate
        for _ in range(3):
            rig.server.push(idle_broadcast())
        await asyncio.sleep(0.1)
        assert len(rig.server.requested("thread/resume")) == 2, (
            "repeated idle broadcasts must not stack concurrent subscription attempts"
        )
        gate.set()
        assert await until(lambda: rig.runtime._subscribed)
        assert rig.runtime._subscribed is True

        # Once subscribed, later idle broadcasts schedule nothing further.
        rig.server.push(idle_broadcast())
        await asyncio.sleep(0.05)
        assert len(rig.server.requested("thread/resume")) == 2

        # Phase 3: the recovered subscription reconciled the completed turn;
        # routing the source batch finishes the SAME job via terminal evidence.
        source, registration = rig.live_registration()
        batch = await source.read()
        evidence = tuple(batch.terminal_evidence)
        assert len(evidence) == 1
        assert evidence[0].native_turn_id == "turn-1"
        assert evidence[0].native_session_id == "ui-thread-1"
        rig.observer._apply_source_batch(
            PARTICIPANT,
            source,
            batch,
            QuietClock(),
            TurnAccumulator(),
            registration=registration,
        )
        await rig.observer._route_terminal_evidence(PARTICIPANT, source, batch, registration)
        assert rig.store.get_job(job.handle).state == JobState.DONE.value
        assert (
            rig.store.get_native_terminal_evidence(
                participant_id=PARTICIPANT,
                backend_generation=GENERATION,
                native_session_id="ui-thread-1",
                native_turn_id="turn-1",
            )
            is not None
        )

        # The stuck job no longer hangs a caller's await: it qualifies as terminal.
        daemon = SimpleNamespace(store=rig.store, jobs=rig.jobs, presence=Absent())
        reasons = await coordinate_await(daemon, parse_targets(daemon, [job.handle]), max_wait=0.02)
        assert reasons[job.handle] == REASON_JOB_TERMINAL
    finally:
        await rig.aclose()
