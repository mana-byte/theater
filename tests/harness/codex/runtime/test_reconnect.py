"""Codex native reconnect recovers missed completions and the rollout race."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from tests.support.codex_server import (
    GENERATION,
    PARTICIPANT,
    ScriptedCodexIO,
    ScriptedCodexServer,
    make_runtime,
    open_new,
    summary_turn_completed,
)
from tests.support.observation import minimal_gates
from tests.support.waiting import until
from theater.daemon.awaiting import REASON_JOB_TERMINAL, coordinate_await, parse_targets
from theater.daemon.controls.service import ControlService
from theater.daemon.jobs import JobManager
from theater.daemon.observation.live import LiveObservationHub, LiveRegistration
from theater.daemon.observation.reducer import QuietClock
from theater.daemon.observation.service import Observer
from theater.daemon.observation.turns import TurnAccumulator
from theater.daemon.registry import Registry
from theater.harness.builtin.plugins.codex import runtime_constants as codex_runtime_constants
from theater.harness.builtin.plugins.codex.manifest import MANIFEST
from theater.harness.channels.hybrid import HybridSource
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.runtime import (
    DeliveryResult,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeExecutionState,
    RuntimeNotification,
    RuntimeRequestError,
    SessionOpenMode,
)
from theater.harness.contracts.source import Batch, Source
from theater.models import JobState, Participant, Tier


async def test_reconnect_recovers_missed_first_completion() -> None:
    server = ScriptedCodexServer()
    # First runtime instance: NEW + send; the turn is accepted.
    first, _binding = await open_new(server)
    receipt = await first.send(operation_id="op-early", prompt="work")
    assert receipt.native_turn_id == "turn-1"
    # The control connection dies before turn/completed arrives (the scripted
    # server never delivers it).
    await first.aclose()
    # Daemon restart: a fresh runtime reconnects to the exact same thread.
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "ui-thread-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "error": None,
                        "items": [{"id": "i-agent", "type": "agentMessage", "text": "recovered"}],
                    }
                ],
            }
        },
    )
    second = make_runtime(server)
    binding = await second.open_session(
        mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1"
    )
    assert binding.native_session_id == "ui-thread-1"
    batch = await second.live_source().read()
    assert len(batch.terminal_evidence) == 1
    outcome = batch.terminal_evidence[0]
    assert outcome.native_turn_id == "turn-1"
    assert outcome.terminal.value == "completed"
    assert outcome.result == "recovered"
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    await second.aclose()


async def test_reconnect_pages_old_terminal_history_with_bounded_backpressure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older exact turns are paged, not lost behind a trailing-window slice."""
    monkeypatch.setattr(codex_runtime_constants, "CODEX_RUNTIME_OUTCOMES_BUFFER", 1)
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {"thread": {"id": "ui-thread-1", "status": {"type": "idle"}, "turns": []}},
    )
    pages = {
        None: {
            "data": [{"id": "human-new", "status": "completed", "items": []}],
            "nextCursor": "middle",
        },
        "middle": {
            "data": [{"id": "human-middle", "status": "completed", "items": []}],
            "nextCursor": "old",
        },
        "old": {
            "data": [{"id": "theater-old", "status": "completed", "items": []}],
            "nextCursor": None,
        },
    }

    def page(params: Mapping[str, object]) -> object:
        assert params["threadId"] == "ui-thread-1"
        return pages[params.get("cursor")]

    server.response_handlers["thread/turns/list"] = page
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
    assert server.requested("thread/resume") == [
        {
            "threadId": "ui-thread-1",
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
    ]
    source = runtime.live_source()

    async def wait_for_pages(count: int) -> None:
        for _ in range(200):
            if len(server.requested("thread/turns/list")) >= count:
                return
            await asyncio.sleep(0.005)
        pytest.fail(f"timed out waiting for history page {count}")

    try:
        await wait_for_pages(2)
        await asyncio.sleep(0.02)
        # The second page is blocked trying to enqueue while the first
        # outcome is unread; it cannot fetch an unbounded third page.
        assert len(server.requested("thread/turns/list")) == 2
        assert runtime._outcomes.qsize() == 1

        (newest,) = (await source.read()).terminal_evidence
        assert newest.native_turn_id == "human-new"

        await wait_for_pages(3)
        (middle,) = (await source.read()).terminal_evidence
        assert middle.native_turn_id == "human-middle"

        for _ in range(200):
            batch = await source.read()
            if batch.terminal_evidence:
                break
            await asyncio.sleep(0.005)
        else:
            pytest.fail("timed out waiting for the old paged terminal turn")
        (oldest,) = batch.terminal_evidence
        assert oldest.native_turn_id == "theater-old"
        assert oldest.native_session_id == "ui-thread-1"
    finally:
        await runtime.aclose()


async def test_reconcile_reports_active_turn_and_last_terminal_turn() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "ui-thread-1",
                "status": {"type": "active"},
                "turns": [
                    {"id": "turn-old", "status": "completed", "items": []},
                    {"id": "turn-live", "status": "inProgress", "items": []},
                ],
            }
        },
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
    snapshot = await runtime.snapshot()
    assert snapshot.native_turn_id == "turn-live"
    batch = await runtime.live_source().read()
    # turn-old is outside the bounded reconcile window? It is the previous
    # turn of the active one, so its missed terminal state is recovered.
    assert [e.native_turn_id for e in batch.terminal_evidence] == ["turn-old"]
    await runtime.aclose()


async def test_reconnect_retries_a_cursor_cycle_and_closes_retry_task(monkeypatch) -> None:
    """Malformed cursors back off and restart without losing the runtime's owned task."""
    monkeypatch.setattr(codex_runtime_constants, "CODEX_RUNTIME_RECONCILE_RETRY_SECONDS", 0.01)
    server = ScriptedCodexServer()

    def page(params):
        if len(server.requested("thread/turns/list")) < 3:
            return {"data": [], "nextCursor": "repeated"}
        return {
            "data": [{"id": "recovered", "status": "completed", "items": []}],
            "nextCursor": None,
        }

    server.response_handlers["thread/turns/list"] = page
    runtime = make_runtime(server)
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        task = runtime._history_reconcile_task
        await asyncio.wait_for(asyncio.shield(task), timeout=2)
        assert len(server.requested("thread/turns/list")) == 3
        assert "cursor" not in server.requested("thread/turns/list")[-1]
        (outcome,) = (await runtime.live_source().read()).terminal_evidence
        assert outcome.native_turn_id == "recovered"
        assert outcome.from_history is True
        assert any(
            "repeated cursor" in text for text in (await runtime.snapshot()).health_diagnostics
        )

        server.fail("thread/turns/list", RuntimeError("temporarily unavailable"))
        runtime._start_history_reconciliation("ui-thread-1")
        retry = runtime._history_reconcile_task
        await asyncio.sleep(0)
        assert not retry.done()
        await runtime.aclose()
        assert retry.done()
    finally:
        await runtime.aclose()


async def test_reconnect_consumes_separate_bounded_initial_turn_page() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {"id": "ui-thread-1", "status": {"type": "active"}, "turns": []},
            "initialTurnsPage": {
                "data": [
                    {"id": "active-page-turn", "status": "inProgress", "items": []},
                    {
                        "id": "ended-page-turn",
                        "status": "interrupted",
                        "items": [],
                        "completedAt": 123.0,
                    },
                ],
                "nextCursor": "older",
            },
        },
    )
    runtime = make_runtime(server)
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        assert server.requested("thread/resume")[0]["excludeTurns"] is True
        assert (await runtime.snapshot()).native_turn_id == "active-page-turn"
        (outcome,) = (await runtime.live_source().read()).terminal_evidence
        assert outcome.native_turn_id == "ended-page-turn"
        assert outcome.from_history is True
        assert outcome.completed_at == 123.0
        assert outcome.completeness is ResultCompleteness.PARTIAL
    finally:
        await runtime.aclose()


async def test_reconnect_revalidates_handshake_subscription_and_evidence_once() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "th-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "error": None,
                        "itemsView": "summary",
                        "items": [
                            {"id": "i-agent", "type": "agentMessage", "text": "recovered final"},
                        ],
                    }
                ],
            }
        },
    )
    io = ScriptedCodexIO(server)
    runtime = make_runtime(server, io=io)
    binding = await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    # Handshake dialect on the reconnecting connection: initialize, then
    # exactly one initialized notification — no protocol requests added.
    assert [method for method, _ in server.requests if method == "initialize"] == ["initialize"]
    assert server.notifications_sent == [("initialized", {})]
    # Exact thread/resume attach-and-subscribe for the exact session, with
    # the original participant/generation identity bound.
    assert server.requested("thread/resume") == [
        {
            "threadId": "th-1",
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
    ]
    assert binding.native_session_id == "th-1"
    assert binding.participant_id == PARTICIPANT
    assert binding.backend_generation == GENERATION
    source = runtime.live_source()
    (outcome,) = (await source.read()).terminal_evidence
    assert outcome.native_session_id == "th-1"
    assert outcome.native_turn_id == "turn-1"
    assert outcome.terminal is NativeTurnTerminal.COMPLETED
    assert outcome.result == "recovered final"
    assert outcome.completeness is ResultCompleteness.PARTIAL
    assert outcome.provenance is ResultProvenance.NATIVE_EVIDENCE
    # No duplicate on replay: the same terminal notification changes nothing.
    server.push(
        summary_turn_completed(
            thread_id="th-1",
            turn_id="turn-1",
            items_view="summary",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "recovered final"}],
        )
    )
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert replay.terminal_evidence == ()
    # The subscription survives the reconnect: a follow-up send never
    # re-subscribes (no second thread/resume).
    receipt = await runtime.send(operation_id="op-2", prompt="more work")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert server.requested("thread/resume") == [
        {
            "threadId": "th-1",
            "excludeTurns": True,
            "initialTurnsPage": {"limit": 2, "itemsView": "summary", "sortDirection": "desc"},
        }
    ]
    snapshot = await runtime.snapshot()
    assert snapshot.participant_id == PARTICIPANT
    assert snapshot.backend_generation == GENERATION
    assert snapshot.native_session_id == "th-1"
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    await runtime.aclose()


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
        # The first send hits the first-turn rollout race.
        await rig.fail_resume_with_no_rollout()
        job = await rig.send()
        assert job.state == JobState.RUNNING.value
        assert rig.runtime._subscribed is False

        # The rollout exists once the turn completed; a successful resume
        # would now reconcile the completed turn as terminal evidence.
        rig.server.failures.pop("thread/resume")
        rig.server.respond("thread/resume", completed_resume_response())

        # Repeated idle broadcasts must schedule exactly one concurrent recovery
        # attempt; the gate holds the request in flight.
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

        # The recovered subscription reconciled the completed turn; routing the source
        # batch finishes the SAME job via terminal evidence.
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
