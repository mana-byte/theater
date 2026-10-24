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
    thread_status_changed,
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
        assert runtime._outcome_buffer.queued_count == 1

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


def unfinished_turn_read_as_interrupted() -> dict:
    """What the app-server returns for a persisted turn it has not seen end: no end time."""
    return {
        "id": "turn-1",
        "status": "interrupted",
        "items": [],
        "completedAt": None,
        "durationMs": None,
    }


async def test_a_running_turn_is_not_taken_for_interrupted_by_the_resume_snapshot_after_send() -> (
    None
):
    """The snapshot taken right after turn/start says the thread is active and the turn ended."""
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {"id": "ui-thread-1", "status": {"type": "active"}},
            "initialTurnsPage": {"data": [unfinished_turn_read_as_interrupted()]},
        },
    )
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        assert runtime._subscribed is True
        assert not (await source.read()).terminal_evidence

        # The exact evidence is the live completion of the same turn.
        server.push(
            summary_turn_completed(
                turn_id="turn-1",
                items_view="summary",
                items=[{"id": "i-final", "type": "agentMessage", "text": "answer"}],
            )
        )
        await asyncio.sleep(0.05)
        (outcome,) = (await source.read()).terminal_evidence
        assert (outcome.native_turn_id, outcome.terminal, outcome.result, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.COMPLETED,
            "answer",
            False,
        )
    finally:
        await runtime.aclose()


async def test_a_running_turn_is_not_taken_for_interrupted_by_a_history_page() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {"id": "ui-thread-1", "status": {"type": "active"}},
            "initialTurnsPage": {"data": [{"id": "turn-1", "status": "inProgress", "items": []}]},
        },
    )
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    runtime = make_runtime(server)
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        await asyncio.wait_for(asyncio.shield(runtime._history_reconcile_task), timeout=2)

        assert len(server.requested("thread/turns/list")) == 1
        assert not (await runtime.live_source().read()).terminal_evidence
    finally:
        await runtime.aclose()


async def test_a_turn_left_unfinished_before_attaching_is_recorded_as_interrupted() -> None:
    """Nothing here holds it as running, so the inferred ending is the truth; recovery keeps it."""
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {"id": "ui-thread-1", "status": {"type": "idle"}},
            "initialTurnsPage": {"data": [unfinished_turn_read_as_interrupted()]},
        },
    )
    runtime = make_runtime(server)
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")

        (outcome,) = (await runtime.live_source().read()).terminal_evidence
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.INTERRUPTED,
            True,
        )
    finally:
        await runtime.aclose()


def active_resume_response(*turns: dict) -> dict:
    """thread/resume answering while the thread is active, newest turn first."""
    return {
        "thread": {"id": "ui-thread-1", "status": {"type": "active", "activeFlags": []}},
        "initialTurnsPage": {"data": list(turns), "nextCursor": None},
    }


def live_completion(turn_id: str = "turn-1") -> RuntimeNotification:
    return summary_turn_completed(
        turn_id=turn_id,
        items_view="summary",
        items=[{"id": f"final-{turn_id}", "type": "agentMessage", "text": "real answer"}],
    )


async def read_outcomes(
    source: Source, *, turn_id: str | None = None, timeout: float = 2.0
) -> list:
    """The terminal evidence the live source has produced, waiting for the first (or the turn's)."""
    outcomes: list = []

    async def arrived() -> bool:
        outcomes.extend((await source.read()).terminal_evidence)
        return any(o.native_turn_id == turn_id for o in outcomes) if turn_id else bool(outcomes)

    deadline = asyncio.get_running_loop().time() + timeout
    while not await arrived() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    return outcomes


async def test_idle_processed_before_a_delayed_send_response_does_not_end_the_turn() -> None:
    """The app-server flips the thread idle before it emits turn/completed (F1)."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", active_resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    server.request_gates["thread/resume"] = gate
    entered = server.request_entered.setdefault("thread/resume", asyncio.Event())
    try:
        send = asyncio.create_task(runtime.send(operation_id="op-1", prompt="work"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        server.push(thread_status_changed("idle"))
        assert await until(lambda: runtime._thread_status == "idle")
        gate.set()
        await send

        assert not (await source.read()).terminal_evidence
        server.push(live_completion())
        (outcome,) = await read_outcomes(source)
        assert (outcome.terminal, outcome.result, outcome.from_history) == (
            NativeTurnTerminal.COMPLETED,
            "real answer",
            False,
        )
    finally:
        await runtime.aclose()


async def test_a_next_turn_started_before_an_older_history_page_does_not_end_the_first() -> None:
    """A page read while turn-1 ran can land after turn-2 became the active turn (F1)."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", active_resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    server.request_gates["thread/turns/list"] = gate
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        runtime._start_history_reconciliation("ui-thread-1")
        await asyncio.wait_for(entered.wait(), timeout=2)
        server.push(
            RuntimeNotification(
                method="turn/started",
                params={
                    "threadId": "ui-thread-1",
                    "turn": {"id": "turn-2", "status": "inProgress"},
                },
            )
        )
        assert await until(lambda: runtime._active_turn_id == "turn-2")
        gate.set()
        await asyncio.wait_for(runtime._history_reconcile_task, timeout=2)

        assert not (await source.read()).terminal_evidence
        server.push(live_completion())
        (outcome,) = await read_outcomes(source)
        assert (outcome.native_turn_id, outcome.terminal) == (
            "turn-1",
            NativeTurnTerminal.COMPLETED,
        )
    finally:
        await runtime.aclose()


async def test_an_attaching_runtime_does_not_end_a_turn_its_response_says_is_running() -> None:
    """A fresh RECONNECT holds no active id, so the response alone must say it (F2)."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", active_resume_response(unfinished_turn_read_as_interrupted()))
    runtime = make_runtime(server)
    source = runtime.live_source()
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")

        assert not (await source.read()).terminal_evidence
        server.push(live_completion())
        (outcome,) = await read_outcomes(source)
        assert (outcome.terminal, outcome.result, outcome.from_history) == (
            NativeTurnTerminal.COMPLETED,
            "real answer",
            False,
        )
    finally:
        await runtime.aclose()


async def test_a_missed_live_completion_is_recovered_from_history_once_the_thread_is_idle() -> None:
    """The turn really ended and only idle was seen live; history now carries its end time (F3)."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", active_resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        assert not (await source.read()).terminal_evidence

        ended = {**unfinished_turn_read_as_interrupted(), "completedAt": 1_780_000_000.0}
        server.respond("thread/turns/list", {"data": [ended], "nextCursor": None})
        server.push(thread_status_changed("idle"))

        (outcome,) = await read_outcomes(source)
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.INTERRUPTED,
            True,
        )
        assert outcome.completed_at == 1_780_000_000.0
        assert len(server.requested("turn/start")) == 1, "recovery never replays a prompt"
    finally:
        await runtime.aclose()


async def test_a_turn_that_started_while_a_page_was_read_is_not_ended_by_that_page() -> None:
    """Asked while idle, the page can still catch a turn that started during the read."""
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {"id": "ui-thread-1", "status": {"type": "idle"}},
            "initialTurnsPage": {"data": []},
        },
    )
    runtime = make_runtime(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
    server.request_gates["thread/turns/list"] = gate
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        await asyncio.wait_for(entered.wait(), timeout=2)
        server.push(
            RuntimeNotification(
                method="turn/started",
                params={
                    "threadId": "ui-thread-1",
                    "turn": {"id": "turn-1", "status": "inProgress"},
                },
            )
        )
        assert await until(lambda: runtime._active_turn_id == "turn-1")
        gate.set()
        await asyncio.wait_for(runtime._history_reconcile_task, timeout=2)

        assert not (await source.read()).terminal_evidence
    finally:
        await runtime.aclose()


def ended_turn(turn_id: str = "turn-1", status: str = "completed") -> dict:
    return {"id": turn_id, "status": status, "items": [], "completedAt": 1_780_000_000.0}


async def idle_pass(server: ScriptedCodexServer, reads: int) -> None:
    """Report idle and wait until the history read it triggers (the ``reads``-th) has been made."""
    server.push(thread_status_changed("idle"))
    assert await until(lambda: len(server.requested("thread/turns/list")) >= reads)
    await asyncio.sleep(0.05)


async def test_idle_before_the_scheduled_backfill_does_not_end_a_deferred_turn() -> None:
    """The backfill asked under idle sees the same snapshot, but its turn was already deferred."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", active_resume_response(unfinished_turn_read_as_interrupted()))
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    runtime = make_runtime(server)
    source = runtime.live_source()
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        await runtime._handle_notification(thread_status_changed("idle"))
        await asyncio.wait_for(runtime._history_reconcile_task, timeout=2)

        assert not (await source.read()).terminal_evidence
        server.push(live_completion())
        (outcome,) = await read_outcomes(source)
        assert (outcome.terminal, outcome.from_history) == (NativeTurnTerminal.COMPLETED, False)
    finally:
        await runtime.aclose()


async def test_an_in_progress_history_read_leaves_the_turn_pending_for_the_next_idle() -> None:
    server = ScriptedCodexServer()
    server.respond("thread/resume", active_resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        server.respond("thread/turns/list", {"data": [ended_turn(status="inProgress")]})
        await idle_pass(server, 1)

        assert runtime._unfinished_turns == {"turn-1"}
        assert not (await source.read()).terminal_evidence

        server.respond("thread/turns/list", {"data": [ended_turn()]})
        await idle_pass(server, 2)
        (outcome,) = await read_outcomes(source)
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.COMPLETED,
            True,
        )
        assert not runtime._unfinished_turns
    finally:
        await runtime.aclose()


async def test_a_turn_deferred_while_a_pass_runs_survives_that_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_runtime_constants, "CODEX_RUNTIME_RECONCILE_RETRY_SECONDS", 0.01)
    server = ScriptedCodexServer()
    server.respond("thread/resume", active_resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        server.request_gates["thread/turns/list"] = gate
        server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
        server.push(thread_status_changed("idle"))
        await asyncio.wait_for(entered.wait(), timeout=2)

        # A second turn is deferred while the pass is in flight.
        second = {**unfinished_turn_read_as_interrupted(), "id": "turn-2"}
        await runtime._reconcile_thread(
            {"id": "ui-thread-1", "status": {"type": "active"}}, "ui-thread-1", turns=[second]
        )
        gate.set()
        await asyncio.sleep(0.2)

        assert runtime._unfinished_turns == {"turn-1", "turn-2"}
        server.respond("thread/turns/list", {"data": [ended_turn("turn-2")]})
        await idle_pass(server, len(server.requested("thread/turns/list")) + 1)
        (outcome,) = await read_outcomes(source, turn_id="turn-2")
        assert (outcome.native_turn_id, outcome.terminal) == (
            "turn-2",
            NativeTurnTerminal.COMPLETED,
        )
        assert runtime._unfinished_turns == {"turn-1"}
    finally:
        await runtime.aclose()


async def test_a_deferred_turn_beyond_the_first_history_page_is_still_examined() -> None:
    """Newer turns pushed it past page one; the paginator follows the cursor to it."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", active_resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    size = codex_runtime_constants.CODEX_RUNTIME_RECONCILE_PAGE_SIZE
    newer = [ended_turn(f"turn-{n}") for n in range(size + 1, 1, -1)]
    older = ended_turn("turn-1")
    server.response_handlers["thread/turns/list"] = lambda params: (
        {"data": newer, "nextCursor": "older"}
        if "cursor" not in params
        else {"data": [older], "nextCursor": None}
    )
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        server.push(thread_status_changed("idle"))

        outcomes = await read_outcomes(source, turn_id="turn-1")
        (first,) = [o for o in outcomes if o.native_turn_id == "turn-1"]
        assert (first.terminal, first.from_history) == (NativeTurnTerminal.COMPLETED, True)
        assert not runtime._unfinished_turns
    finally:
        await runtime.aclose()
