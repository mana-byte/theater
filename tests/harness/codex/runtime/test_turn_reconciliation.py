"""Codex native never ends a running turn from history, and recovers one whose end was lost."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Mapping

import pytest

from tests.support.codex_server import (
    ScriptedCodexServer,
    make_runtime,
    open_new,
    summary_turn_completed,
    thread_status_changed,
)
from tests.support.waiting import until, wait_until
from theater.harness.builtin.plugins.codex import runtime_constants as codex_runtime_constants
from theater.harness.contracts.runtime import (
    NativeTurnTerminal,
    RuntimeNotification,
    SessionOpenMode,
)
from theater.harness.contracts.source import Source


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
        (outcome,) = await read_outcomes(source)
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
        await asyncio.wait_for(asyncio.shield(runtime._history_reconcile_task), timeout=10)

        assert len(server.requested("thread/turns/list")) == 1
        assert not (await runtime.live_source().read()).terminal_evidence
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("completed_at", [None, "bad", True, -1, float("nan")])
async def test_an_unfinished_turn_on_attach_never_manufactures_a_terminal(completed_at) -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {"id": "ui-thread-1", "status": {"type": "idle"}},
            "initialTurnsPage": {
                "data": [{**unfinished_turn_read_as_interrupted(), "completedAt": completed_at}]
            },
        },
    )
    runtime = make_runtime(server)
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")

        assert not (await runtime.live_source().read()).terminal_evidence
    finally:
        await runtime.aclose()


def resume_response(*turns: dict, status: str = "active") -> dict:
    """thread/resume as the app-server answers it, newest turn first."""
    return {
        "thread": {"id": "ui-thread-1", "status": {"type": status, "activeFlags": []}},
        "initialTurnsPage": {"data": list(turns), "nextCursor": None},
    }


def ended_turn(
    turn_id: str = "turn-1", status: str = "completed", completed_at: float = 1_780_000_000.0
) -> dict:
    return {"id": turn_id, "status": status, "items": [], "completedAt": completed_at}


def turn_started(turn_id: str) -> RuntimeNotification:
    return RuntimeNotification(
        method="turn/started",
        params={"threadId": "ui-thread-1", "turn": {"id": turn_id, "status": "inProgress"}},
    )


def live_completion(turn_id: str = "turn-1") -> RuntimeNotification:
    return summary_turn_completed(
        turn_id=turn_id,
        items_view="summary",
        items=[{"id": f"final-{turn_id}", "type": "agentMessage", "text": "real answer"}],
    )


async def read_outcomes(
    source: Source, *, turn_id: str | None = None, timeout: float = 5.0
) -> list:
    """The terminal evidence the live source has produced, waiting for the first (or the turn's)."""
    outcomes: list = []

    def arrived() -> bool:
        buffered = source.buffered_terminal_evidence()
        return any(o.native_turn_id == turn_id for o in buffered) if turn_id else bool(buffered)

    await wait_until(arrived, timeout=timeout, what="terminal evidence to arrive")
    outcomes.extend((await source.read()).terminal_evidence)
    return outcomes


async def history_pass_done(runtime) -> None:
    """Wait for the owned history task to end: a barrier for what its passes did or did not do."""
    await asyncio.wait_for(asyncio.shield(runtime._history_reconcile_task), timeout=10)


@pytest.fixture
def short_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Milliseconds, not zero: a completion handled right after idle must still land inside it."""
    monkeypatch.setattr(codex_runtime_constants, "CODEX_RUNTIME_RECONCILE_IDLE_GRACE_SECONDS", 0.01)


@pytest.fixture
def fast_rereads(monkeypatch: pytest.MonkeyPatch, short_grace: None) -> None:
    monkeypatch.setattr(codex_runtime_constants, "CODEX_RUNTIME_RECONCILE_RETRY_SECONDS", 0.005)


@pytest.mark.parametrize("status", ["idle", "active"])
async def test_a_resume_response_reading_the_acknowledged_turn_as_over_does_not_end_it(
    status: str,
) -> None:
    """Stock 0.163.0 answered thread/resume for a running turn this way with either thread status.

    Captured over 300 same-connection turns: 7 unfinished snapshots said idle, 3 said active; every
    one of those turns then completed.
    """
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume", resume_response(unfinished_turn_read_as_interrupted(), status=status)
    )
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    try:
        await runtime.send(operation_id="op-1", prompt="work")

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


async def test_idle_processed_before_a_delayed_send_response_does_not_end_the_turn() -> None:
    """The app-server flips the thread idle before it emits turn/completed."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    server.request_gates["thread/resume"] = gate
    entered = server.request_entered.setdefault("thread/resume", asyncio.Event())
    try:
        send = asyncio.create_task(runtime.send(operation_id="op-1", prompt="work"))
        await asyncio.wait_for(entered.wait(), timeout=10)
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
    """A page read while turn-1 ran can land after turn-2 became the active turn."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    server.request_gates["thread/turns/list"] = gate
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        runtime._start_history_reconciliation("ui-thread-1")
        await asyncio.wait_for(entered.wait(), timeout=10)
        server.push(turn_started("turn-2"))
        assert await until(lambda: runtime._active_turn_id == "turn-2")
        gate.set()
        await history_pass_done(runtime)

        assert not (await source.read()).terminal_evidence
        server.push(live_completion())
        (outcome,) = await read_outcomes(source)
        assert (outcome.native_turn_id, outcome.terminal) == (
            "turn-1",
            NativeTurnTerminal.COMPLETED,
        )
    finally:
        await runtime.aclose()


async def test_a_send_acknowledged_while_an_idle_page_is_in_flight_is_not_ended_by_that_page() -> (
    None
):
    """The page was asked while idle and returns after turn/start was acknowledged."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(status="idle"))
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    runtime = make_runtime(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    server.request_gates["thread/turns/list"] = gate
    entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        await asyncio.wait_for(entered.wait(), timeout=10)
        await runtime.send(operation_id="op-1", prompt="work")
        gate.set()
        await history_pass_done(runtime)

        assert not (await source.read()).terminal_evidence
        server.push(live_completion())
        (outcome,) = await read_outcomes(source)
        assert (outcome.terminal, outcome.from_history) == (NativeTurnTerminal.COMPLETED, False)
    finally:
        await runtime.aclose()


async def test_an_attaching_runtime_does_not_end_a_turn_its_response_says_is_running() -> None:
    """A fresh RECONNECT holds no turn id, so the response alone must say it is running."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
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


async def test_a_turn_that_started_while_a_page_was_read_is_not_ended_by_that_page() -> None:
    """Asked while idle, the page can still catch a turn that started during the read."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(status="idle"))
    runtime = make_runtime(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
    server.request_gates["thread/turns/list"] = gate
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        await asyncio.wait_for(entered.wait(), timeout=10)
        server.push(turn_started("turn-1"))
        assert await until(lambda: runtime._active_turn_id == "turn-1")
        gate.set()
        await history_pass_done(runtime)

        assert not (await source.read()).terminal_evidence
    finally:
        await runtime.aclose()


async def test_a_newer_turn_the_page_shows_before_its_notice_is_not_ended_by_that_page() -> None:
    """turn-1 ended and a human started turn-2; the page arrives before either notification."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.respond(
        "thread/turns/list",
        {"data": [{**unfinished_turn_read_as_interrupted(), "id": "turn-2"}, ended_turn()]},
    )
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        runtime._start_history_reconciliation("ui-thread-1")
        await history_pass_done(runtime)

        (first,) = (await source.read()).terminal_evidence
        assert (first.native_turn_id, first.terminal, first.from_history) == (
            "turn-1",
            NativeTurnTerminal.COMPLETED,
            True,
        )
        server.push(turn_started("turn-2"))
        server.push(live_completion("turn-2"))
        (outcome,) = await read_outcomes(source, turn_id="turn-2")
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-2",
            NativeTurnTerminal.COMPLETED,
            False,
        )
    finally:
        await runtime.aclose()


async def test_a_real_interrupt_with_an_end_time_is_recorded_while_the_thread_reads_active() -> (
    None
):
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        resume_response({**unfinished_turn_read_as_interrupted(), "completedAt": 1_780_000_000.0}),
    )
    runtime, _binding = await open_new(server)
    try:
        await runtime.send(operation_id="op-1", prompt="work")

        (outcome,) = await read_outcomes(runtime.live_source())
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.INTERRUPTED,
            True,
        )
        assert outcome.completed_at == 1_780_000_000.0
        assert not runtime._in_flight_turns
    finally:
        await runtime.aclose()


@pytest.mark.parametrize("stale_reads", [0, 2])
async def test_a_lost_live_completion_is_recovered_from_history_once_the_thread_is_idle(
    stale_reads: int, fast_rereads: None
) -> None:
    """Idle was seen but turn/completed was not; history shows the end, maybe after a stale read."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    reads: list[int] = []

    def page(_params: Mapping[str, object]) -> object:
        reads.append(len(reads))
        stale = len(reads) <= stale_reads
        return {
            "data": [
                unfinished_turn_read_as_interrupted() if stale else ended_turn(status="interrupted")
            ]
        }

    server.response_handlers["thread/turns/list"] = page
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        assert not (await source.read()).terminal_evidence
        server.push(thread_status_changed("idle"))

        (outcome,) = await read_outcomes(source)
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.INTERRUPTED,
            True,
        )
        assert outcome.completed_at == 1_780_000_000.0
        assert len(reads) == stale_reads + 1
        assert len(server.requested("turn/start")) == 1, "recovery never replays a prompt"
    finally:
        await runtime.aclose()


async def test_an_attached_turns_lost_completion_is_recovered_from_history(
    fast_rereads: None,
) -> None:
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    runtime = make_runtime(server)
    source = runtime.live_source()
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        await history_pass_done(runtime)
        assert not (await source.read()).terminal_evidence

        server.respond("thread/turns/list", {"data": [ended_turn()]})
        server.push(thread_status_changed("idle"))
        (outcome,) = await read_outcomes(source)
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.COMPLETED,
            True,
        )
    finally:
        await runtime.aclose()


async def test_idle_followed_by_its_completion_costs_no_history_read(short_grace: None) -> None:
    """The app-server emits turn/completed within tens of ms of idle: nothing is left to read."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        await runtime._handle_notification(thread_status_changed("idle"))
        await runtime._handle_notification(live_completion())
        await history_pass_done(runtime)

        assert not server.requested("thread/turns/list")
        (outcome,) = await read_outcomes(source)
        assert (outcome.terminal, outcome.from_history) == (NativeTurnTerminal.COMPLETED, False)
    finally:
        await runtime.aclose()


async def test_a_second_idle_during_a_pass_does_not_lose_the_end_that_pass_missed(
    fast_rereads: None,
) -> None:
    """The first read answers with a state older than the end; a second idle finds the pass busy."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
    reads: list[int] = []

    def page(_params: Mapping[str, object]) -> object:
        reads.append(len(reads))
        return {
            "data": [unfinished_turn_read_as_interrupted() if len(reads) == 1 else ended_turn()]
        }

    server.response_handlers["thread/turns/list"] = page
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        server.request_gates["thread/turns/list"] = gate
        server.push(thread_status_changed("idle"))
        await asyncio.wait_for(entered.wait(), timeout=10)
        server.push(thread_status_changed("idle"))
        assert await until(lambda: not server.pending)
        gate.set()

        (outcome,) = await read_outcomes(source)
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.COMPLETED,
            True,
        )
        assert len(reads) == 2
    finally:
        await runtime.aclose()


async def test_idle_rereads_never_release_a_turn_without_positive_terminal_evidence(
    fast_rereads: None,
) -> None:
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    recorded_before_each_read: list[int] = []

    def page(_params: Mapping[str, object]) -> object:
        recorded_before_each_read.append(runtime._outcome_buffer.queued_count)
        return {"data": [unfinished_turn_read_as_interrupted()]}

    server.response_handlers["thread/turns/list"] = page
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        server.push(thread_status_changed("idle"))

        assert await until(lambda: len(recorded_before_each_read) >= 8)
        assert not (await source.read()).terminal_evidence
        assert runtime._in_flight_turns == {"turn-1"}
        assert all(count == 0 for count in recorded_before_each_read)
        server.push(live_completion())
        (outcome,) = await read_outcomes(source)
        assert outcome.terminal is NativeTurnTerminal.COMPLETED and not outcome.from_history
        await history_pass_done(runtime)
        assert not runtime._in_flight_turns
    finally:
        await runtime.aclose()


async def test_turns_in_flight_survive_a_pass_that_ends_because_the_thread_is_active_again(
    fast_rereads: None,
) -> None:
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    gate = asyncio.Event()
    entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        server.request_gates["thread/turns/list"] = gate
        server.push(thread_status_changed("idle"))
        await asyncio.wait_for(entered.wait(), timeout=10)
        server.push(turn_started("turn-2"))
        assert await until(lambda: runtime._active_turn_id == "turn-2")
        gate.set()
        await history_pass_done(runtime)

        assert runtime._in_flight_turns == {"turn-1", "turn-2"}
        assert not (await source.read()).terminal_evidence
        server.push(live_completion("turn-1"))
        server.push(live_completion("turn-2"))
        outcomes = await read_outcomes(source, turn_id="turn-2")
        assert {(o.native_turn_id, o.terminal, o.from_history) for o in outcomes} == {
            ("turn-1", NativeTurnTerminal.COMPLETED, False),
            ("turn-2", NativeTurnTerminal.COMPLETED, False),
        }
        assert not runtime._in_flight_turns
    finally:
        await runtime.aclose()


async def test_closing_the_runtime_ends_the_rereads(short_grace: None) -> None:
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    runtime, _binding = await open_new(server)
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        server.push(thread_status_changed("idle"))
        assert await until(lambda: len(server.requested("thread/turns/list")) == 1)
        task = runtime._history_reconcile_task

        await runtime.aclose()

        assert task.cancelled() and runtime._history_reconcile_task is None
        assert len(server.requested("thread/turns/list")) == 1
    finally:
        await runtime.aclose()


@pytest.mark.parametrize(
    "order", list(itertools.permutations(("resume", "page", "idle", "completed"))), ids="-".join
)
async def test_no_delivery_order_ends_the_turn_or_keeps_it_in_flight_after_its_completion(
    order: tuple[str, ...], short_grace: None
) -> None:
    """A delayed resume response and history page, idle and the live completion, in any order."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    server.respond("thread/turns/list", {"data": [unfinished_turn_read_as_interrupted()]})
    runtime, _binding = await open_new(server)
    resume_gate, page_gate = asyncio.Event(), asyncio.Event()
    server.request_gates.update({"thread/resume": resume_gate, "thread/turns/list": page_gate})
    try:
        send = asyncio.create_task(runtime.send(operation_id="op-1", prompt="work"))
        resume_entered = server.request_entered.setdefault("thread/resume", asyncio.Event())
        await asyncio.wait_for(resume_entered.wait(), timeout=10)
        page = asyncio.create_task(runtime._page_history("ui-thread-1"))
        page_entered = server.request_entered.setdefault("thread/turns/list", asyncio.Event())
        await asyncio.wait_for(page_entered.wait(), timeout=10)

        for step in order:
            if step == "resume":
                resume_gate.set()
                await send
            elif step == "page":
                page_gate.set()
                await asyncio.wait_for(page, timeout=10)
            elif step == "idle":
                await runtime._handle_notification(thread_status_changed("idle"))
            else:
                await runtime._handle_notification(live_completion())

        outcomes = [
            (o.native_turn_id, o.terminal, o.from_history)
            for o in (await runtime.live_source().read()).terminal_evidence
        ]
        assert outcomes == [("turn-1", NativeTurnTerminal.COMPLETED, False)]
        assert not runtime._in_flight_turns
    finally:
        resume_gate.set()
        page_gate.set()
        await runtime.aclose()


async def test_a_deferred_turn_beyond_the_first_history_page_is_still_examined(
    short_grace: None,
) -> None:
    """Newer turns pushed it past page one; the paginator follows the cursor to it."""
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
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
        assert not runtime._in_flight_turns
    finally:
        await runtime.aclose()


async def test_a_newer_unfinished_turn_a_page_shows_beside_a_held_one_is_held_too(
    fast_rereads: None,
) -> None:
    """A page that lists the held turn and a newer unfinished one: the newer cannot be over."""
    server = ScriptedCodexServer()
    running = {"id": "turn-1", "status": "inProgress", "items": []}
    newer = {**unfinished_turn_read_as_interrupted(), "id": "human-2"}
    server.respond("thread/resume", resume_response(running))
    server.respond("thread/turns/list", {"data": [newer, running]})
    runtime = make_runtime(server)
    source = runtime.live_source()
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        await history_pass_done(runtime)

        assert not (await source.read()).terminal_evidence
        assert runtime._in_flight_turns == {"turn-1", "human-2"}
        server.push(live_completion("human-2"))
        (outcome,) = await read_outcomes(source, turn_id="human-2")
        assert (outcome.terminal, outcome.from_history) == (NativeTurnTerminal.COMPLETED, False)
    finally:
        await runtime.aclose()


async def test_a_completion_landing_inside_the_idle_grace_costs_no_history_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grace is what keeps a PARTIAL history outcome from beating the live COMPLETE one."""
    monkeypatch.setattr(codex_runtime_constants, "CODEX_RUNTIME_RECONCILE_IDLE_GRACE_SECONDS", 0.3)
    server = ScriptedCodexServer()
    server.respond("thread/resume", resume_response(unfinished_turn_read_as_interrupted()))
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    try:
        await runtime.send(operation_id="op-1", prompt="work")
        await runtime._handle_notification(thread_status_changed("idle"))
        for _ in range(10):  # the history task starts and parks inside its grace
            await asyncio.sleep(0)
        assert not server.requested("thread/turns/list"), "the grace was skipped"

        await runtime._handle_notification(live_completion())
        await history_pass_done(runtime)

        assert not server.requested("thread/turns/list")
        (outcome,) = await read_outcomes(source)
        assert (outcome.terminal, outcome.from_history) == (NativeTurnTerminal.COMPLETED, False)
    finally:
        await runtime.aclose()


async def test_an_attached_in_progress_turn_whose_end_was_missed_is_recovered_from_history(
    fast_rereads: None,
) -> None:
    """A snapshot's inProgress turn is a running turn the runtime must see through to its end."""
    server = ScriptedCodexServer()
    running = {"id": "turn-1", "status": "inProgress", "items": []}
    server.respond("thread/resume", resume_response(running))
    server.respond("thread/turns/list", {"data": [running]})
    runtime = make_runtime(server)
    source = runtime.live_source()
    try:
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
        await history_pass_done(runtime)
        assert runtime._in_flight_turns == {"turn-1"}

        server.respond("thread/turns/list", {"data": [ended_turn()]})
        server.push(thread_status_changed("idle"))
        (outcome,) = await read_outcomes(source)
        assert (outcome.native_turn_id, outcome.terminal, outcome.from_history) == (
            "turn-1",
            NativeTurnTerminal.COMPLETED,
            True,
        )
    finally:
        await runtime.aclose()
