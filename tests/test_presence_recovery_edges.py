"""Presence recovery boundaries using real registry semantics and scripted tmux."""

from __future__ import annotations

import asyncio

from sqlalchemy import delete

from tests.test_presence_monitor import (
    Clock,
    FakeRegistry,
    PresenceScript,
    make_client,
    make_inventory,
    participant,
    wire,
)
from theater.daemon.presence import PresenceMonitor, PresenceState
from theater.daemon.schema import participants
from theater.models import Status


async def test_rearm_cannot_resurrect_absence_after_inventory_failure(monkeypatch):
    clock = Clock()
    script = PresenceScript([make_inventory(clock)], clock)
    wire(monkeypatch, script)
    monitor = PresenceMonitor(FakeRegistry(participant()), clock=clock)
    await monitor.refresh()
    assert not monitor.snapshot("p1").protected
    script.failure = RuntimeError("inventory unavailable")
    await monitor.refresh()
    script.ensure_failure = RuntimeError("option unavailable")
    await monitor._arm(force=True)
    assert monitor.snapshot("p1").state is PresenceState.UNKNOWN
    await monitor.aclose()


async def test_arm_verification_protects_before_awaiting_option_repair(monkeypatch):
    clock = Clock()
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = PresenceMonitor(FakeRegistry(participant()), clock=clock)
    monitor._trust.arm_ok = True
    monitor._publish(make_inventory(clock, clients=[make_client()]))
    monitor._publish(make_inventory(clock, clients=[make_client(focused=False)]))
    assert not monitor.snapshot("p1").protected
    entered, release = asyncio.Event(), asyncio.Event()
    original = script.ensure_focus_events

    async def parked_ensure():
        entered.set()
        await release.wait()
        return await original()

    monkeypatch.setattr("theater.tmux.presence.ensure_focus_events", parked_ensure)
    task = asyncio.create_task(monitor._arm(force=True))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert monitor.snapshot("p1").protected
    finally:
        release.set()
        await task
        await monitor.aclose()


async def test_dead_or_pruned_targets_release_but_a_retained_visible_pane_still_protects(registry):
    clock = Clock()
    inventory = make_inventory(clock, clients=[make_client()])
    target = registry.create_spawned(harness="pi", cwd="/tmp")
    monitor = PresenceMonitor(registry, clock=clock)
    assert monitor.snapshot(target.id).state is PresenceState.ABSENT
    registry.attach_pane(target.id, "%1", tmux_server_identity=inventory.server_identity)
    registry.mark_dead(target.id)
    monitor._publish(inventory)
    assert registry.get(target.id).status is Status.DEAD
    assert monitor.snapshot(target.id).state is PresenceState.PRESENT
    monitor._publish(make_inventory(clock, panes={}, pane_pids={}))
    assert monitor.snapshot(target.id).state is PresenceState.ABSENT
    registry.store.conn.execute(delete(participants).where(participants.c.id == target.id))
    assert monitor.snapshot(target.id).state is PresenceState.ABSENT


async def test_slow_inventory_is_not_published_as_fresh(monkeypatch):
    clock = Clock()
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = PresenceMonitor(FakeRegistry(participant()), clock=clock, stale_after=2)

    async def slow_inventory():
        inventory = make_inventory(clock)
        clock.advance(3)
        return inventory

    monkeypatch.setattr("theater.tmux.presence.observe_focus_inventory", slow_inventory)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.UNKNOWN
    assert monitor.snapshot("p1").reason == "stale-inventory"
    await monitor.aclose()
