"""Daemon composition of the provider-backed presence monitor."""

from __future__ import annotations

import pytest

from theater.daemon.presence.contracts import PresenceState
from theater.daemon.server import Daemon
from theater.models import HumanPresent


async def test_startup_arms_and_observes_before_controls(theater_home, terminal_provider):
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
        assert daemon.presence.revision >= 1
    finally:
        await daemon.aclose()


async def test_registered_participant_reads_absent_then_present(daemon, terminal_provider):
    participant = daemon.registry.register(harness="pi", pane=None, cwd="/tmp")
    terminal_id = terminal_provider.bind(daemon, participant.id, command="pi")
    await daemon.presence.refresh()
    assert daemon.presence.snapshot(participant.id).state is PresenceState.ABSENT

    terminal_provider.presence[terminal_id] = "present"
    await daemon.presence.refresh()
    with pytest.raises(HumanPresent):
        await daemon.presence.require_absent(participant.id)

    terminal_provider.presence[terminal_id] = "absent"
    await daemon.presence.refresh()
    await daemon.presence.require_absent(participant.id)


async def test_reconcile_refreshes_provider_evidence(daemon, terminal_provider):
    participant = daemon.registry.register(harness="pi", pane=None, cwd="/tmp")
    terminal_provider.bind(daemon, participant.id, command="pi")
    revisions_before = daemon.presence.revision
    await daemon._reconcile()
    assert daemon.presence.revision > revisions_before


async def test_shutdown_stops_monitor(theater_home, terminal_provider):
    daemon = Daemon(harnesses={})
    await daemon.start()
    assert daemon.presence._loop_task is not None
    await daemon.aclose()
    assert daemon.presence._loop_task is None
