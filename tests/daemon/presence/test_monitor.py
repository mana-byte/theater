"""Presence snapshots, absence-access helpers and the monitor lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from tests.support.provider_presence import Clock, Registry, TerminalService, binding, result
from theater.constants.refusals import NEXT_FOCUS_EVENTS_OFF, NEXT_RETRY_ONCE
from theater.daemon.presence import PresenceMonitor, PresenceSnapshot, PresenceState
from theater.daemon.presence.access import check_absent, presence_snapshot, require_absent
from theater.daemon.server import Daemon
from theater.models import HumanPresent, Participant


@pytest.mark.parametrize(
    ("state", "protected"),
    [
        (PresenceState.PRESENT, True),
        (PresenceState.UNKNOWN, True),
        (PresenceState.ABSENT, False),
    ],
)
def test_snapshot_protects_everything_except_confirmed_absence(
    state: PresenceState, protected: bool
) -> None:
    snapshot = PresenceSnapshot(state, "focus", 7, 123.5)

    assert snapshot.protected is protected


def test_snapshot_serializes_the_exact_shared_wire_shape() -> None:
    snapshot = PresenceSnapshot(PresenceState.UNKNOWN, "never observed", 3, None)

    assert snapshot.to_dict() == {
        "state": "unknown",
        "protected": True,
        "reason": "never observed",
        "revision": 3,
        "observed_at": None,
    }


def test_snapshot_is_immutable() -> None:
    snapshot = PresenceSnapshot(PresenceState.ABSENT, "blurred", 2, 123.5)

    with pytest.raises(FrozenInstanceError):
        snapshot.state = PresenceState.PRESENT  # type: ignore[misc]


async def test_missing_provider_never_grants_mutation_or_projects_absence():
    daemon = SimpleNamespace()
    assert presence_snapshot(daemon, "p1").state is PresenceState.UNKNOWN
    with pytest.raises(HumanPresent):
        check_absent(daemon, "p1")
    with pytest.raises(HumanPresent, match="no composed presence provider"):
        await require_absent(daemon, "p1")


def test_projection_failure_is_unknown_without_extra_io():
    def failed_snapshot(participant_id):
        raise RuntimeError("cache unavailable")

    daemon = SimpleNamespace(presence=SimpleNamespace(snapshot=failed_snapshot))
    result = presence_snapshot(daemon, "p1")
    assert result.state is PresenceState.UNKNOWN
    assert result.protected


@pytest.mark.parametrize(
    ("reason", "next_step"),
    [("focused", "leave the pane"), ("no-terminal-presence-evidence", "no terminal exists")],
)
def test_a_presence_refusal_names_the_next_step(reason: str, next_step: str) -> None:
    snapshot = PresenceSnapshot(PresenceState.UNKNOWN, reason, 1, None)
    daemon = SimpleNamespace(presence=SimpleNamespace(snapshot=lambda _pid: snapshot))
    with pytest.raises(HumanPresent) as refused:
        check_absent(daemon, "p1")
    assert refused.value.details is not None
    assert next_step in refused.value.details["next"]


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


async def test_identical_reports_do_not_bump_revision(daemon, terminal_provider):
    participant = daemon.registry.register(harness="pi", pane=None, cwd="/tmp")
    terminal_id = terminal_provider.bind(daemon, participant.id, command="pi")
    terminal_provider.presence[terminal_id] = "absent"
    await daemon.presence.refresh()
    settled = daemon.presence.revision
    await daemon.presence.refresh()
    assert daemon.presence.revision == settled
    terminal_provider.presence[terminal_id] = "present"
    await daemon.presence.refresh()
    assert daemon.presence.revision > settled


async def test_shutdown_stops_monitor(theater_home, terminal_provider):
    daemon = Daemon(harnesses={})
    await daemon.start()
    assert daemon.presence._loop_task is not None
    await daemon.aclose()
    assert daemon.presence._loop_task is None


def _monitor(*, transient_wait: float = 5.0):
    registry = Registry(Participant(id="participant-a", harness="pi"), binding())
    service = TerminalService(registry)
    monitor = PresenceMonitor(
        registry, clock=Clock(), stale_after=5.0, transient_wait=transient_wait
    )
    monitor.configure_terminal_service(service)
    return monitor, service


def _go_stale(service, monkeypatch) -> None:
    service.connections.generation = 8
    monkeypatch.setattr(service.connections, "current_generation", lambda _provider: None)


async def test_stale_generation_that_recovers_inside_the_bound_is_admitted(monkeypatch):
    monitor, service = _monitor()
    _go_stale(service, monkeypatch)

    async def provider_reconnects() -> None:
        await asyncio.sleep(0.3)
        service.connections.generation = 7
        service.responses.append(result("absent", 1))

    reconnect = asyncio.create_task(provider_reconnects())
    try:
        await asyncio.wait_for(monitor.require_absent("participant-a"), 5)
    finally:
        await reconnect
        await monitor.aclose()
    assert service.inspect_calls == 1  # absence came from one fresh inspection


async def test_stale_generation_past_the_bound_refuses_with_retry_once(monkeypatch):
    monitor, service = _monitor(transient_wait=0.3)
    _go_stale(service, monkeypatch)
    with pytest.raises(HumanPresent) as refused:
        await asyncio.wait_for(monitor.require_absent("participant-a"), 5)
    assert "provider-generation-stale" in str(refused.value)
    assert refused.value.details == {"next": NEXT_RETRY_ONCE}
    await monitor.aclose()


async def test_focus_events_unavailable_is_refused_without_waiting():
    monitor, service = _monitor()
    unreadable = result("unknown", 1)
    unreadable["presence"]["reason"] = "focus_events_unavailable"
    service.responses.append(unreadable)
    with pytest.raises(HumanPresent) as refused:
        await asyncio.wait_for(monitor.require_absent("participant-a"), 5)
    assert refused.value.details == {"next": NEXT_FOCUS_EVENTS_OFF}
    assert service.inspect_calls == 1
    await monitor.aclose()
