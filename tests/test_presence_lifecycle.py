"""Daemon composition of the presence monitor: startup, reconcile, shutdown."""

from __future__ import annotations

import pytest

from theater.constants.presence import PRESENCE_WAKE_CHANNEL
from theater.daemon.presence.contracts import PresenceState
from theater.daemon.server import Daemon
from theater.models import HumanPresent


def register_pane_participant(daemon, pane="%1"):
    """Register a pane owner whose pid the fake inventory does not bind."""
    return daemon.registry.register(harness="pi", pane=pane, cwd="/tmp")


async def test_startup_arms_and_observes_before_controls(theater_home, fake_tmux):
    """The monitor is armed and has observed once before controls dispatch."""
    daemon = Daemon(harnesses={})
    spy_seen = {}
    original_start = daemon.controls.start

    def spy(participant_ids=()):
        spy_seen["presence_live"] = (
            daemon.presence._loop_task is not None and not daemon.presence._loop_task.done()
        )
        return original_start(participant_ids)

    daemon.controls.start = spy
    try:
        await daemon.start()
    finally:
        daemon.controls.start = original_start
    try:
        assert spy_seen["presence_live"] is True
        assert fake_tmux.hook_installs == [PRESENCE_WAKE_CHANNEL]
        assert fake_tmux.focus_events_calls >= 1
        assert daemon.presence.revision >= 1
    finally:
        await daemon.aclose()


async def test_registered_participant_reads_absent_then_present(daemon, fake_tmux):
    """Reconcile stamps the pane owner; a focused viewer makes it present."""
    participant = register_pane_participant(daemon)
    # Reconcile stamps the pane owner's server identity after registration.
    await daemon._reconcile()
    stamped = daemon.registry.get(participant.id)
    assert stamped.tmux_server_identity == fake_tmux.tmux_server_identity
    await daemon.presence.refresh()
    assert daemon.presence.snapshot(participant.id).state is PresenceState.ABSENT

    fake_tmux.add_focus_client(window_id="@0", active_pane_id="%1")
    with pytest.raises(HumanPresent):
        await daemon.presence.require_absent(participant.id)

    fake_tmux.focus_clients.clear()
    await daemon.presence.require_absent(participant.id)


async def test_reconcile_re_arms_and_refreshes(daemon, fake_tmux):
    calls_before = fake_tmux.focus_events_calls
    revisions_before = daemon.presence.revision
    await daemon._reconcile()
    assert fake_tmux.focus_events_calls == calls_before + 1
    assert daemon.presence.revision > revisions_before


async def test_shutdown_sweeps_hooks_and_stops_monitor(theater_home, fake_tmux):
    """aclose sweeps owned hooks and leaves no monitor tasks behind."""
    daemon = Daemon(harnesses={})
    await daemon.start()
    assert daemon.presence._loop_task is not None
    await daemon.aclose()
    assert fake_tmux.hook_removals == [PRESENCE_WAKE_CHANNEL]
    assert daemon.presence._loop_task is None
    assert daemon.presence._waiter_task is None
