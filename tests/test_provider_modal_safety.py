"""Provider prompt submission refuses dialogs and unproven screens."""

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_control_service import Harness, make_runtime, state_of
from theater.harness import get as get_harness
from theater.harness.contracts.runtime import SessionOpenMode
from theater.models import AwaitingDecision, Busy, JobState, StaleTarget, Status

SCREENS = Path(__file__).parent / "fixtures" / "screens"

#: Stock Codex 0.158 trust dialog rendered by the provider fixture.
TRUST_MENU_0158 = """\
Trust this folder?
Working with untrusted contents carries risk.

› 1. Trust and continue
  2. Start with restrictions

  Press enter to continue
"""

#: An interactive menu that no classifier knows: never a ready composer.
UNKNOWN_MENU = """\
  Approval settings

› 1. Always ask
  2. Auto-approve edits

Press enter to confirm or esc to go back
"""


def _spawn(daemon, terminal_provider, *, screen):
    participant = daemon.registry.create_spawned(harness="codex", cwd="/repo")
    terminal = terminal_provider.bind(daemon, participant.id, command="codex")
    terminal_provider.presence[terminal] = "absent"
    if screen is not None:
        terminal_provider.screens[terminal] = screen
    return participant, terminal


@pytest.mark.parametrize(
    ("screen", "expected_kind"),
    [
        ((SCREENS / "codex_trust.txt").read_text(), "trust"),
        ((SCREENS / "codex_approval.txt").read_text(), "approval"),
        (TRUST_MENU_0158, "trust"),
    ],
    ids=["old-trust-fixture", "approval-fixture", "stock-0.158-trust"],
)
async def test_send_refuses_dialog_even_with_stale_idle_status(
    daemon, terminal_provider, screen, expected_kind
):
    """A modal refuses delivery regardless of the stored display status."""
    participant, _terminal = _spawn(daemon, terminal_provider, screen=screen)
    daemon.registry.set_status(participant.id, Status.IDLE)

    with pytest.raises(AwaitingDecision) as raised:
        await daemon.controls.send(participant.id, caller_id="cli", prompt="Investigate only")

    assert expected_kind in str(raised.value)
    assert terminal_provider.deliveries == []
    assert daemon.store.active_running_jobs_for_target(participant.id) == []


async def test_send_refuses_missing_capture_evidence(daemon, terminal_provider):
    """No screen evidence can never manufacture prompt readiness."""
    participant, _terminal = _spawn(daemon, terminal_provider, screen=None)

    with pytest.raises(Busy):
        await daemon.controls.send(participant.id, caller_id="cli", prompt="Investigate only")

    assert terminal_provider.deliveries == []
    assert daemon.store.active_running_jobs_for_target(participant.id) == []


async def test_send_refuses_failed_capture(daemon, terminal_provider, monkeypatch):
    """A provider capture error refuses delivery instead of failing open."""
    participant, _terminal = _spawn(
        daemon, terminal_provider, screen=(SCREENS / "codex_idle.txt").read_text()
    )

    async def exploding(_participant_id, *, max_bytes):
        raise RuntimeError("provider inspect broke")

    monkeypatch.setattr(daemon.presence, "capture_screen", exploding)

    with pytest.raises(Busy):
        await daemon.controls.send(participant.id, caller_id="cli", prompt="Investigate only")

    assert terminal_provider.deliveries == []


async def test_send_refuses_classifier_failure(daemon, terminal_provider, monkeypatch):
    """A harness classifier error refuses delivery instead of failing open."""
    participant, _terminal = _spawn(
        daemon, terminal_provider, screen=(SCREENS / "codex_idle.txt").read_text()
    )

    def exploding(_capture):
        raise RuntimeError("classifier broke")

    monkeypatch.setattr(get_harness("codex").observer, "screen_reading", exploding)

    try:
        with pytest.raises(Busy):
            await daemon.controls.send(participant.id, caller_id="cli", prompt="Investigate")
    finally:
        monkeypatch.undo()

    assert terminal_provider.deliveries == []
    assert daemon.store.active_running_jobs_for_target(participant.id) == []


async def test_send_refuses_unknown_menu_state(daemon, terminal_provider):
    """An unrecognized interactive menu is not a composer; refuse it."""
    participant, _terminal = _spawn(daemon, terminal_provider, screen=UNKNOWN_MENU)

    with pytest.raises(Busy):
        await daemon.controls.send(participant.id, caller_id="cli", prompt="Investigate only")

    assert terminal_provider.deliveries == []
    assert daemon.store.active_running_jobs_for_target(participant.id) == []


async def test_send_refuses_when_binding_changes_during_capture(
    daemon, terminal_provider, monkeypatch
):
    """Screen evidence of a previous terminal never authorizes delivery."""
    participant, _terminal = _spawn(
        daemon, terminal_provider, screen=(SCREENS / "codex_idle.txt").read_text()
    )
    original = daemon.presence.capture_screen

    async def capture_then_rebind(participant_id, *, max_bytes):
        capture = await original(participant_id, max_bytes=max_bytes)
        with daemon.store.write_unit() as unit:
            unit.connection.execute(
                text(
                    "UPDATE terminal_bindings SET terminal_incarnation = 'incarnation-moved'"
                    " WHERE participant_id = :pid"
                ),
                {"pid": participant_id},
            )
        return capture

    monkeypatch.setattr(daemon.presence, "capture_screen", capture_then_rebind)

    with pytest.raises(StaleTarget):
        await daemon.controls.send(participant.id, caller_id="cli", prompt="Investigate only")

    assert terminal_provider.deliveries == []
    assert daemon.store.active_running_jobs_for_target(participant.id) == []


async def test_send_delivers_on_a_proven_ready_prompt(daemon, terminal_provider):
    """A high-confidence prompt composer delivers exactly once."""
    participant, terminal = _spawn(
        daemon, terminal_provider, screen=(SCREENS / "codex_idle.txt").read_text()
    )

    job = await daemon.controls.send(participant.id, caller_id="cli", prompt="Investigate only")

    assert terminal_provider.deliveries == [(terminal, "Investigate only")]
    assert job.state == JobState.RUNNING
    running = daemon.store.active_running_jobs_for_target(participant.id)
    assert [item.handle for item in running] == [job.handle]


@pytest.mark.parametrize("screen", [TRUST_MENU_0158, UNKNOWN_MENU], ids=["dialog", "unknown-menu"])
async def test_queued_followup_defers_then_recovers(daemon, terminal_provider, screen):
    """Dispatch refuses and stays queued until a real prompt appears."""
    participant, terminal = _spawn(daemon, terminal_provider, screen=screen)
    job = await daemon.controls.queue_followup(
        participant.id, caller_id="cli", prompt="Then do this"
    )

    deferred = await daemon.controls.dispatch_queue(participant.id)

    assert deferred.dispatched == ()
    assert deferred.deferred is True
    assert terminal_provider.deliveries == []
    assert [op.operation_id for op in daemon.store.queued_control_operations(participant.id)]
    assert daemon.store.get_job(job.handle).state == JobState.RUNNING

    terminal_provider.screens[terminal] = (SCREENS / "codex_idle.txt").read_text()
    # The daemon's own maintenance may win the recovery race; either way the
    # queued prompt is delivered exactly once and the queue drains.
    for _ in range(200):
        if terminal_provider.deliveries:
            break
        await asyncio.sleep(0.005)

    assert terminal_provider.deliveries == [(terminal, "Then do this")]
    assert daemon.store.queued_control_operations(participant.id) == []
    assert daemon.store.get_job(job.handle).state == JobState.RUNNING


async def test_native_route_never_consults_the_provider_modal_gate(store):
    """A vindictive modal gate must not affect native protocol delivery."""
    runtime = make_runtime("p1")
    await runtime.open_session(mode=SessionOpenMode.NEW)
    harness = Harness(store, {"p1": runtime})

    async def vindictive(participant_id):
        raise Busy(f"native route of {participant_id!r} must not consult the modal gate")

    harness.service._gates = replace(harness.service._gates, provider_modal_check=vindictive)

    job = await harness.service.send("p1", caller_id="caller", prompt="do the thing")

    assert job.state == JobState.RUNNING
    assert state_of(harness, "p1").sent == ["do the thing"]
