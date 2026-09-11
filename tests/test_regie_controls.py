"""Régie participant controls: steer, queue, settings, inspect, interrupt.

Two things are under test. The `ControlController` unit tests cover the
background-action contract: per-operation connections, per-target
coalescing, and refusal after close. The app tests drive the real Textual
pilot against a faked daemon and prove the responsiveness property with
deterministic barriers — while participant A's control (or kill cleanup) is
held behind an `asyncio.Event`, input still processes, polling still
completes, and participant B's control still finishes.

Daemon-derived capability reasons and pending/unknown delivery states are
asserted on the presentation helpers, which must never guess support locally.
"""

from __future__ import annotations

import asyncio

import pytest

from theater.config import Config, RegieSection
from theater.protocol import RemoteError
from theater.regie import app as app_mod
from theater.regie.app import RegieApp
from theater.regie.controllers import controls as controls_mod
from theater.regie.controllers.controls import (
    ControlController,
    ControlOutcome,
    describe_interrupt,
    describe_receipt,
    describe_settings,
    format_controls_report,
)
from theater.regie.palette import SessionCommands

PARENT = {
    "id": "aaaaaaaaaaaa",
    "tier": "spawned",
    "harness": "codex",
    "status": "working",
    "cwd": "/tmp/proj",
    "tmux_pane": "%10",
    "addressable": True,
    "children": [],
}

CHILD = {
    "id": "bbbbbbbbbbbb",
    "tier": "spawned",
    "harness": "vibe",
    "status": "idle",
    "cwd": "/tmp/proj/child",
    "tmux_pane": "%11",
    "addressable": True,
    "children": [],
}


# ---- controller (no Textual) -----------------------------------------------


class FakeClient:
    """A DaemonClient that answers from a dict and remembers what was asked."""

    def __init__(self, answers: dict, broken: set[str]):
        self.answers = answers
        self.broken = broken
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    async def connect(self) -> None:
        pass

    async def call(self, method: str, **params):
        import inspect

        self.calls.append((method, params))
        if method in self.broken:
            raise RuntimeError(f"{method} is unavailable")
        answer = self.answers.get(method, {})
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            answer = answer(params)
        if inspect.isawaitable(answer):
            answer = await answer
        return answer

    async def aclose(self) -> None:
        self.closed = True

    def asked(self, method: str) -> list[dict]:
        return [p for m, p in self.calls if m == method]


class RecordingFactory:
    """DaemonClient stand-in that hands the test every client it created."""

    def __init__(self, answers: dict, broken: set[str]):
        self.answers = answers
        self.broken = broken
        self.clients: list[FakeClient] = []

    def __call__(self, *args, **kwargs) -> FakeClient:
        client = FakeClient(self.answers, self.broken)
        self.clients.append(client)
        return client


async def _settle() -> None:
    """Let created tasks run to their first await and back."""
    for _ in range(4):
        await asyncio.sleep(0)


async def test_each_control_opens_and_closes_its_own_connection():
    factory = RecordingFactory({}, set())
    controller = ControlController(factory)
    outcomes: list[ControlOutcome] = []

    async def on_done(outcome: ControlOutcome) -> None:
        outcomes.append(outcome)

    assert controller.steer("p_1", "more context", on_done=on_done) is True
    await _settle()
    assert len(factory.clients) == 1
    client = factory.clients[0]
    assert client.asked("participant.steer") == [
        {"target": "p_1", "prompt": "more context", "caller_id": "cli"}
    ]
    assert client.closed
    assert outcomes[0].ok is True
    assert controller.in_flight == frozenset()


async def test_repeated_same_target_actions_are_refused_and_distinct_ones_run():
    gate = asyncio.Event()

    async def slow_steer(_params):
        await gate.wait()
        return {"delivery": "accepted"}

    factory = RecordingFactory({"participant.steer": slow_steer}, set())
    controller = ControlController(factory)

    assert controller.steer("p_1", "first") is True
    await _settle()
    # Same participant, same action: refused explicitly, never queued.
    assert controller.steer("p_1", "second") is False
    # Same action, other participant: must not wait behind p_1's barrier.
    assert controller.steer("p_2", "other session") is True
    # Same participant, different action: independent.
    assert controller.interrupt("p_1") is True
    await _settle()

    # Both steers are blocked on the daemon answer, but on two connections,
    # and the interrupt finished without waiting behind either of them.
    assert controller.in_flight == {("p_1", "steer"), ("p_2", "steer")}
    steer_clients = [c for c in factory.clients if c.asked("participant.steer")]
    assert len(steer_clients) == 2
    assert controller.in_flight is not None
    await controller.aclose()
    gate.set()


async def test_a_control_failure_carries_the_daemon_reason():
    refusal = RemoteError(
        "not_supported", "steer is unavailable: legacy wiring has no native control"
    )
    factory = RecordingFactory({"participant.steer": refusal}, set())
    controller = ControlController(factory)
    outcomes: list[ControlOutcome] = []

    async def on_done(outcome: ControlOutcome) -> None:
        outcomes.append(outcome)

    assert controller.steer("p_1", "message", on_done=on_done) is True
    await _settle()
    assert outcomes[0].ok is False
    assert "legacy wiring has no native control" in outcomes[0].error
    assert factory.clients[0].closed


async def test_aclose_cancels_pending_controls_and_closes_their_clients():
    gate = asyncio.Event()

    async def slow_inspect(_params):
        await gate.wait()
        return {}

    factory = RecordingFactory({"participant.controls": slow_inspect}, set())
    controller = ControlController(factory)
    assert controller.inspect("p_1") is True
    await _settle()
    await controller.aclose()
    assert factory.clients[0].closed
    assert controller.inspect("p_1") is False
    gate.set()


async def test_a_closed_controller_refuses_every_action():
    controller = ControlController(RecordingFactory({}, set()))
    await controller.aclose()
    assert controller.steer("p_1", "m") is False
    assert controller.queue_followup("p_1", "p") is False
    assert controller.update_settings("p_1", model="m2") is False
    assert controller.interrupt("p_1") is False
    assert controller.inspect("p_1") is False


async def test_queue_and_settings_send_their_exact_params():
    factory = RecordingFactory({}, set())
    controller = ControlController(factory)
    assert controller.queue_followup("p_1", "then do this") is True
    assert controller.update_settings("p_1", model="gpt-5.6", reasoning_effort="high") is True
    await _settle()
    assert factory.clients[0].asked("participant.queue_followup") == [
        {"target": "p_1", "prompt": "then do this", "caller_id": "cli"}
    ]
    assert factory.clients[1].asked("participant.settings.update") == [
        {
            "target": "p_1",
            "caller_id": "cli",
            "model": "gpt-5.6",
            "reasoning_effort": "high",
        }
    ]


# ---- daemon-derived presentation ------------------------------------------


def test_receipts_distinguish_pending_and_unknown_delivery() -> None:
    """The exact participant.steer shape: an additive delivery word plus details."""
    assert describe_receipt("steer", {"delivery": "accepted"}) == (
        "steer accepted",
        "information",
    )
    assert describe_receipt("steer", {"delivery": "pending"}) == (
        "steer pending — delivery not confirmed yet",
        "warning",
    )
    assert describe_receipt(
        "steer",
        {"delivery": "unknown", "phase": "ack_pending", "reason": "ack_timeout"},
    ) == (
        "steer delivery unknown — the daemon will reconcile (ack_timeout; ack_pending)",
        "warning",
    )
    assert describe_receipt("steer", {"delivery": "rejected", "reason": "job_not_amendable"}) == (
        "steer rejected (job_not_amendable)",
        "error",
    )
    # An unfamiliar delivery word is shown verbatim, never flattened.
    assert describe_receipt("steer", {"delivery": "in_flight_amendment"}) == (
        "steer in_flight_amendment",
        "information",
    )
    assert describe_receipt("steer", None) == ("steer accepted", "information")


def test_settings_receipts_never_render_refusal_or_unknown_as_success() -> None:
    """The exact participant.settings.update shape: applied is three-valued."""
    assert describe_settings({"applied": True}) == (
        "settings updated",
        "information",
    )
    assert describe_settings(
        {"applied": False, "error_code": "busy", "error": "session is mid-turn"}
    ) == (
        "settings update refused (busy; session is mid-turn)",
        "error",
    )
    assert describe_settings({"applied": None}) == (
        "settings update outcome unknown — the daemon will reconcile",
        "warning",
    )
    # A missing or non-dict answer is an unknown outcome, never a claimed success.
    assert describe_settings(None)[1] == "warning"
    assert describe_settings({})[1] == "warning"


def test_a_queued_followup_shows_the_daemon_handle() -> None:
    assert describe_receipt("queue", {"handle": "bbbbbbbbbbbb#7"}) == (
        "followup queued as bbbbbbbbbbbb#7",
        "information",
    )
    # A queue receipt without a handle still reports the delivery word.
    assert describe_receipt("queue", {"delivery": "accepted"}) == (
        "queue accepted",
        "information",
    )


def test_interrupt_receipts_keep_the_existing_rpc_vocabulary() -> None:
    assert describe_interrupt({"interrupted": True}) == ("interrupted", "information")
    assert describe_interrupt({"interrupted": False, "reason": "already_not_working"}) == (
        "nothing to interrupt — already_not_working",
        "information",
    )
    assert describe_interrupt(None) == ("interrupted", "information")


def test_the_controls_report_shows_daemon_capability_reasons() -> None:
    """The exact participant.controls shape, verbatim, without local inference."""
    report = format_controls_report(
        {
            "wiring": "native",
            "health": {"connection": "ok", "diagnostics": []},
            "capabilities": {
                "steer": {"available": True},
                "queue_followup": {
                    "available": False,
                    "reason": "not_idle",
                    "detail": "session is mid-turn",
                },
                "settings_update": {"available": False, "reason": "unsupported_model"},
                "interrupt": {"available": True},
            },
            "settings": {"model": "gpt-5.6", "reasoning_effort": "high"},
            "active_turn": {"native_turn_id": "turn-9"},
            "queued": ["aaaaaaaaaaaa#3", "aaaaaaaaaaaa#4"],
        }
    )
    assert "wiring: native" in report
    assert "health: connection=ok" in report
    # Empty diagnostics stay absent rather than rendering an empty line.
    assert "diagnostics:" not in report
    assert "steer: available" in report
    assert "queue_followup: unavailable — not_idle (session is mid-turn)" in report
    assert "settings_update: unavailable — unsupported_model" in report
    assert "interrupt: available" in report
    assert "settings: model=gpt-5.6, reasoning_effort=high" in report
    assert "active turn: turn-9" in report
    assert "queued followups: 2 (aaaaaaaaaaaa#3, aaaaaaaaaaaa#4)" in report


def test_health_diagnostics_render_verbatim() -> None:
    report = format_controls_report(
        {
            "health": {
                "connection": "degraded",
                "diagnostics": ["backend ack late", "frame gap at t3"],
            }
        }
    )
    assert report.split("\n") == [
        "health: connection=degraded",
        "diagnostics: backend ack late; frame gap at t3",
    ]


def test_the_controls_report_never_invents_facts() -> None:
    assert format_controls_report(None) == "the daemon reported no controls"
    assert format_controls_report({}) == "the daemon reported no controls"
    # Boolean capability entries and absent sections render without guessing.
    report = format_controls_report({"capabilities": {"steer": False}})
    assert report == "steer: unavailable — no reason given"


def test_the_controls_report_is_bounded() -> None:
    huge = {f"cap_{i}": {"available": False, "reason": "x" * 500} for i in range(40)}
    report = format_controls_report({"capabilities": huge})
    lines = report.split("\n")
    assert len(lines) <= controls_mod.REGIE_CONTROLS_REPORT_MAX_LINES
    assert all(len(line) <= controls_mod.REGIE_CONTROLS_REPORT_LINE_MAX for line in lines)


# ---- app wiring, barriers, and the palette ---------------------------------


@pytest.fixture
def daemon(monkeypatch):
    """Install a fake DaemonClient factory and hand the test the recorders."""
    state: dict = {
        "answers": {
            "participants.tree": [dict(PARENT, children=[dict(CHILD)])],
            "participants.unmanaged": [],
            "bus.tail": [],
            "harnesses": [
                {"name": "codex", "approvals": ["manual", "edits", "yolo"]},
                {"name": "vibe", "approvals": ["manual", "edits", "yolo"]},
            ],
            "usage_summary": {
                "all_time": {},
                "windowed": {},
                "average": {},
            },
        },
        "broken": set(),
        "clients": [],
    }

    def factory(*_args, **_kwargs):
        client = FakeClient(state["answers"], state["broken"])
        state["clients"].append(client)
        return client

    monkeypatch.setattr(app_mod, "DaemonClient", factory)
    return state


@pytest.fixture
def tmux(monkeypatch):
    """Fake the tmux surface the app touches; record the calls in order."""
    calls: list[tuple] = []

    async def display_message(fmt, *, target=None):
        return {
            "#{window_id}": "@7",
            "#{session_id}": "$2",
            "#{session_name}": "work",
        }[fmt]

    async def show_option(name, *, target):
        return None

    async def set_option(name, value, *, target):
        calls.append(("set", name, value))

    async def unset_option(name, *, target):
        calls.append(("unset", name))

    async def join_pane(pane, *, target_window=None):
        calls.append(("join", pane, target_window))

    async def break_pane(pane, *, target_window=None):
        calls.append(("break", pane))

    async def pane_exists(_pane):
        return True

    async def resize_pane(pane, *, width=None):
        calls.append(("resize", pane, width))

    async def select_pane(pane):
        calls.append(("select", pane))

    async def bind_key_if_free(table, key, command, *, note):
        calls.append(("bind", table, key, tuple(command)))
        return True

    async def unbind_key_if_owned(table, key, *, note):
        calls.append(("unbind", table, key))

    monkeypatch.setattr(app_mod.tmux, "current_pane", lambda: "%1")
    monkeypatch.setattr(app_mod.tmux, "display_message", display_message)
    monkeypatch.setattr(app_mod.tmux, "show_option", show_option)
    monkeypatch.setattr(app_mod.tmux, "set_option", set_option)
    monkeypatch.setattr(app_mod.tmux, "unset_option", unset_option)
    monkeypatch.setattr(app_mod.tmux, "bind_key_if_free", bind_key_if_free)
    monkeypatch.setattr(app_mod.tmux, "unbind_key_if_owned", unbind_key_if_owned)
    monkeypatch.setattr(app_mod.panes, "join_pane", join_pane)
    monkeypatch.setattr(app_mod.panes, "break_pane", break_pane)
    monkeypatch.setattr(app_mod.panes, "pane_exists", pane_exists)
    monkeypatch.setattr(app_mod.panes, "resize_pane", resize_pane)
    monkeypatch.setattr(app_mod.panes, "select_pane", select_pane)
    return calls


def make_app(**regie) -> tuple[RegieApp, list[tuple[str, str]]]:
    """An app with slow timers, and the list its notifications land in."""
    values = {"tree_interval": 60, "bus_interval": 60, "startup_reveal": False}
    values.update(regie)
    settings = Config(regie=RegieSection(**values))
    app = RegieApp(settings)
    notes: list[tuple[str, str]] = []
    app.notify = lambda msg, **kw: notes.append(  # type: ignore[method-assign]
        (str(msg), kw.get("severity", "information"))
    )
    return app, notes


def control_calls(daemon, method: str) -> list[tuple[FakeClient, dict]]:
    """Every recorded call to *method* across all clients the app created."""
    return [
        (client, params)
        for client in daemon["clients"]
        for name, params in client.calls
        if name == method
    ]


def gated(gate: asyncio.Event, answer: dict | None = None, *, target: str | None = None):
    """A daemon answer that blocks on *gate*, optionally only for *target*."""

    async def respond(params):
        if target is None or params.get("target") == target:
            await gate.wait()
        return {} if answer is None else answer

    return respond


async def wait_for_control(daemon, method: str, count: int = 1) -> None:
    """Deterministic arrival wait: poll the recorded calls, never the clock."""
    for _ in range(500):
        if len(control_calls(daemon, method)) >= count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"never saw {count} {method} requests")


async def test_a_blocked_steer_never_stalls_input_polling_or_the_other_participant(daemon, tmux):
    """The Wave 4C responsiveness property, held behind a real barrier."""
    gate = asyncio.Event()
    daemon["answers"]["participant.steer"] = gated(
        gate, answer={"delivery": "accepted"}, target=PARENT["id"]
    )
    daemon["answers"]["participant.queue_followup"] = {"handle": f"{CHILD['id']}#7"}
    app, notes = make_app()
    async with app.run_test() as pilot:
        # Participant A (the parent) steers, and the daemon sits on it.
        app.action_steer_session()
        await pilot.pause()
        modal = app.screen
        assert type(modal).__name__ == "ControlPromptScreen"
        await asyncio.wait_for(pilot.press(*"hold the line"), timeout=5)
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await wait_for_control(daemon, "participant.steer", 1)
        steer_client = control_calls(daemon, "participant.steer")[0][0]
        assert steer_client.asked("participant.steer") == [
            {
                "target": PARENT["id"],
                "prompt": "hold the line",
                "caller_id": "cli",
            }
        ]
        assert control_calls(daemon, "participant.steer")[0][1]["target"] == PARENT["id"]

        # Repeating the same steer is refused explicitly, not queued.
        app.action_steer_session()
        await asyncio.wait_for(pilot.press(*"again"), timeout=5)
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await asyncio.wait_for(pilot.pause(), timeout=5)
        assert len(control_calls(daemon, "participant.steer")) == 1
        assert any("steer already under way" in msg for msg, _ in notes)

        # Input still processes while the steer is blocked.
        await asyncio.wait_for(pilot.press("j"), timeout=5)
        assert app.cursor == 1

        # Polling still completes while the steer is blocked.
        tree_before = len(daemon["clients"][0].asked("participants.tree"))
        await asyncio.wait_for(app._refresh_tree(), timeout=5)
        assert len(daemon["clients"][0].asked("participants.tree")) == tree_before + 1

        # Participant B's control completes on its own connection.
        app.action_queue_followup()
        await asyncio.wait_for(pilot.press(*"second opinion"), timeout=5)
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await wait_for_control(daemon, "participant.queue_followup", 1)
        queue_client = control_calls(daemon, "participant.queue_followup")[0][0]
        assert queue_client is not steer_client
        assert queue_client.asked("participant.queue_followup") == [
            {
                "target": CHILD["id"],
                "prompt": "second opinion",
                "caller_id": "cli",
            }
        ]
        await wait_for_control(daemon, "participant.steer", 1)
        for _ in range(200):
            if any("followup queued as bbbbbbbbbbbb#7" in msg for msg, _ in notes):
                break
            await asyncio.sleep(0.01)
        assert any("followup queued as bbbbbbbbbbbb#7" in msg for msg, _ in notes)

        # Releasing the barrier lets A's steer finish with its receipt.
        gate.set()
        for _ in range(200):
            if any("steer accepted" in msg for msg, _ in notes):
                break
            await asyncio.sleep(0.01)
        assert any("steer accepted" in msg for msg, _ in notes)
        assert steer_client.closed


async def test_a_blocked_kill_cleanup_never_stalls_another_control_or_polling(daemon, tmux):
    """The accepted kill-controller path, replayed against a second control."""
    kill_gate = asyncio.Event()
    daemon["answers"]["participant.kill"] = gated(kill_gate)
    daemon["answers"]["participant.interrupt"] = {"id": CHILD["id"], "interrupted": True}
    app, notes = make_app()
    async with app.run_test() as pilot:
        # Kill the parent; the daemon's teardown stays behind the barrier.
        await asyncio.wait_for(pilot.press("x"), timeout=5)
        await wait_for_control(daemon, "participant.kill", 1)

        # Input and polling stay alive during the blocked cleanup.
        await asyncio.wait_for(pilot.press("j"), timeout=5)
        assert app.cursor == 1
        tree_before = len(daemon["clients"][0].asked("participants.tree"))
        await asyncio.wait_for(app._refresh_tree(), timeout=5)
        assert len(daemon["clients"][0].asked("participants.tree")) == tree_before + 1

        # The other participant's interrupt completes on its own connection.
        app.action_interrupt_session()
        await wait_for_control(daemon, "participant.interrupt", 1)
        for _ in range(200):
            if any("interrupted" in msg for msg, _ in notes):
                break
            await asyncio.sleep(0.01)
        assert any("interrupted" in msg for msg, _ in notes)
        assert control_calls(daemon, "participant.interrupt")[0][1] == {
            "target": CHILD["id"],
            "caller_id": "cli",
        }

        kill_gate.set()
        await wait_for_control(daemon, "participants.tree", 2)


async def test_a_refused_control_shows_the_daemon_reason(daemon, tmux):
    refusal = RemoteError(
        "not_supported", "participant is on legacy wiring; steer needs native wiring"
    )
    daemon["answers"]["participant.steer"] = refusal
    app, notes = make_app()
    async with app.run_test() as pilot:
        app.action_steer_session()
        await asyncio.wait_for(pilot.press(*"amend"), timeout=5)
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await wait_for_control(daemon, "participant.steer", 1)
        for _ in range(200):
            if any(sev == "error" for _, sev in notes):
                break
            await asyncio.sleep(0.01)
        assert any(
            "steer failed" in msg and "legacy wiring" in msg for msg, sev in notes if sev == "error"
        ), notes


async def test_session_controls_report_shows_daemon_reasons(daemon, tmux):
    daemon["answers"]["participant.controls"] = {
        "wiring": "legacy",
        "health": {"connection": "disconnected", "diagnostics": ["backend socket gone"]},
        "capabilities": {
            "steer": {"available": False, "reason": "no_runtime", "detail": "no runtime attached"},
            "queue_followup": {"available": False, "reason": "needs_native_wiring"},
            "settings_update": {"available": False, "reason": "needs_native_wiring"},
            "interrupt": {"available": True},
        },
        "queued": [{"handle": "aaaaaaaaaaaa#3"}],
    }
    app, notes = make_app()
    async with app.run_test():
        app.action_session_controls()
        await wait_for_control(daemon, "participant.controls", 1)
        for _ in range(200):
            if notes:
                break
            await asyncio.sleep(0.01)
        assert control_calls(daemon, "participant.controls")[0][1] == {
            "target": PARENT["id"],
            "caller_id": "cli",
        }
        message = notes[0][0]
        assert "wiring: legacy" in message
        assert "health: connection=disconnected" in message
        assert "diagnostics: backend socket gone" in message
        assert "steer: unavailable — no_runtime (no runtime attached)" in message
        assert "queue_followup: unavailable — needs_native_wiring" in message
        assert "settings_update: unavailable — needs_native_wiring" in message
        assert "interrupt: available" in message
        assert "queued followups: 1 (aaaaaaaaaaaa#3)" in message


async def test_settings_update_sends_model_and_reasoning(daemon, tmux):
    daemon["answers"]["participant.settings.update"] = {"applied": True}
    app, notes = make_app()
    async with app.run_test() as pilot:
        app.action_update_session_settings()
        await asyncio.wait_for(pilot.press(*"gpt-5.6"), timeout=5)
        await asyncio.wait_for(pilot.press("tab"), timeout=5)
        await asyncio.wait_for(pilot.press(*"high"), timeout=5)
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await wait_for_control(daemon, "participant.settings.update", 1)
        assert control_calls(daemon, "participant.settings.update")[0][1] == {
            "target": PARENT["id"],
            "caller_id": "cli",
            "model": "gpt-5.6",
            "reasoning_effort": "high",
        }
        for _ in range(200):
            if notes:
                break
            await asyncio.sleep(0.01)
        assert notes[0] == ("settings updated", "information")


async def test_a_refused_settings_update_is_an_error_not_a_success(daemon, tmux):
    """applied=False is a definitive refusal; it must never read as accepted."""
    daemon["answers"]["participant.settings.update"] = {
        "applied": False,
        "error_code": "busy",
        "error": "session is mid-turn",
    }
    app, notes = make_app()
    async with app.run_test() as pilot:
        app.action_update_session_settings()
        await asyncio.wait_for(pilot.press(*"gpt-5.6"), timeout=5)
        await asyncio.wait_for(pilot.press("tab"), timeout=5)
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await wait_for_control(daemon, "participant.settings.update", 1)
        for _ in range(200):
            if notes:
                break
            await asyncio.sleep(0.01)
        assert notes[0] == ("settings update refused (busy; session is mid-turn)", "error")


async def test_an_unknown_settings_outcome_is_a_warning_not_a_success(daemon, tmux):
    """applied=None means the daemon will reconcile; the régie says so."""
    daemon["answers"]["participant.settings.update"] = {"applied": None}
    app, notes = make_app()
    async with app.run_test() as pilot:
        app.action_update_session_settings()
        await asyncio.wait_for(pilot.press(*"gpt-5.6"), timeout=5)
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await wait_for_control(daemon, "participant.settings.update", 1)
        for _ in range(200):
            if notes:
                break
            await asyncio.sleep(0.01)
        assert notes[0] == (
            "settings update outcome unknown — the daemon will reconcile",
            "warning",
        )


async def test_an_unknown_steer_delivery_is_a_warning(daemon, tmux):
    """The steer receipt's unknown delivery word surfaces as a warning."""
    daemon["answers"]["participant.steer"] = {
        "delivery": "unknown",
        "phase": "ack_pending",
        "reason": "ack_timeout",
    }
    app, notes = make_app()
    async with app.run_test() as pilot:
        app.action_steer_session()
        await asyncio.wait_for(pilot.press(*"amend"), timeout=5)
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await wait_for_control(daemon, "participant.steer", 1)
        for _ in range(200):
            if notes:
                break
            await asyncio.sleep(0.01)
        assert notes[0] == (
            "steer delivery unknown — the daemon will reconcile (ack_timeout; ack_pending)",
            "warning",
        )


async def test_an_empty_settings_prompt_is_refused_without_a_call(daemon, tmux):
    app, notes = make_app()
    async with app.run_test() as pilot:
        app.action_update_session_settings()
        await asyncio.wait_for(pilot.press("enter"), timeout=5)
        await asyncio.wait_for(pilot.pause(), timeout=5)
        assert control_calls(daemon, "participant.settings.update") == []
        assert any("give a model or a reasoning effort" in msg for msg, _ in notes)


async def test_escaping_the_prompt_sends_nothing(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        app.action_steer_session()
        await asyncio.wait_for(pilot.press(*"never mind"), timeout=5)
        await asyncio.wait_for(pilot.press("escape"), timeout=5)
        await asyncio.wait_for(pilot.pause(), timeout=5)
        assert control_calls(daemon, "participant.steer") == []


async def test_controls_without_a_selection_warn_and_never_call(daemon, tmux):
    daemon["answers"]["participants.tree"] = []
    daemon["answers"]["participants.unmanaged"] = [
        {"pane": "%20", "command": "vibe", "harness": "vibe", "cwd": "/tmp/x"}
    ]
    app, notes = make_app()
    async with app.run_test():
        app.action_interrupt_session()
        app.action_steer_session()
        app.action_session_controls()
        assert control_calls(daemon, "participant.interrupt") == []
        assert control_calls(daemon, "participant.steer") == []
        assert control_calls(daemon, "participant.controls") == []
    assert any("nothing to interrupt" in msg for msg, _ in notes)
    assert any("nothing to steer" in msg for msg, _ in notes)
    assert any("nothing to inspect" in msg for msg, _ in notes)


async def test_pending_controls_are_cancelled_and_the_controller_cleared_on_unmount(daemon, tmux):
    gate = asyncio.Event()
    daemon["answers"]["participant.controls"] = gated(gate)
    app, _ = make_app()
    async with app.run_test():
        app.action_session_controls()
        await wait_for_control(daemon, "participant.controls", 1)
        client = control_calls(daemon, "participant.controls")[0][0]
    # The `async with` exit unmounts the app: the task must cancel, its client
    # close, and the controller be discarded so a remount cannot reuse it.
    assert client.closed
    assert app._controls_controller is None
    gate.set()


async def test_interrupt_receipt_from_the_daemon_is_reported_verbatim(daemon, tmux):
    daemon["answers"]["participant.interrupt"] = {
        "id": CHILD["id"],
        "interrupted": False,
        "reason": "already_not_working",
    }
    app, notes = make_app()
    async with app.run_test() as pilot:
        await asyncio.wait_for(pilot.press("j"), timeout=5)
        app.action_interrupt_session()
        await wait_for_control(daemon, "participant.interrupt", 1)
        for _ in range(200):
            if notes:
                break
            await asyncio.sleep(0.01)
        assert notes[0] == ("nothing to interrupt — already_not_working", "information")


async def test_the_palette_offers_controls_only_with_a_selection(daemon, tmux):
    app, _ = make_app()
    async with app.run_test() as pilot:
        provider = SessionCommands(app.screen, None)
        hits = [hit async for hit in provider.discover()]
        displays = [hit.display for hit in hits]
        assert "Interrupt session" in displays
        assert "Steer session" in displays
        assert "Queue followup" in displays
        assert "Update session settings" in displays
        assert "Session controls" in displays

        await asyncio.wait_for(pilot.press("j"), timeout=5)
        daemon["answers"]["participants.tree"] = []
        await app._refresh_tree()
        provider_empty = SessionCommands(app.screen, None)
        assert [hit async for hit in provider_empty.discover()] == []


async def test_the_control_provider_searches_by_name(daemon, tmux):
    app, _ = make_app()
    async with app.run_test():
        provider = SessionCommands(app.screen, None)
        hits = [hit async for hit in provider.search("steer")]
        assert hits
        assert any("Steer session" in str(hit.match_display) for hit in hits)
        misses = [hit async for hit in provider.search("xyzzy")]
        assert misses == []
