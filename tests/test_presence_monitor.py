"""Deterministic PresenceMonitor tests against scripted inventories."""

from __future__ import annotations

import asyncio

import pytest

import theater.tmux.presence as tmux_presence
from theater.constants.presence import PRESENCE_WAKE_CHANNEL
from theater.daemon.presence.contracts import PresenceState
from theater.daemon.presence.monitor import PresenceMonitor
from theater.models import Busy, HumanPresent, Participant
from theater.tmux.presence import FocusClient, FocusInventory

IDENT = '["/tmp/sock","101","1"]'
OTHER_IDENT = '["/tmp/sock","202","2"]'


def make_client(
    *,
    window_id="@0",
    active_pane_id="%1",
    focused=True,
    readonly=False,
    control=False,
    tty="/dev/ttys001",
    pid="501",
):
    flags = {"attached"}
    if focused:
        flags.add("focused")
    return FocusClient(
        tty=tty,
        pid=pid,
        created="1789162985",
        flags=frozenset(flags),
        readonly=readonly,
        control=control,
        window_id=window_id,
        active_pane_id=active_pane_id,
        termfeatures=frozenset({"focus"}),
    )


def make_inventory(
    *,
    identity=IDENT,
    panes=None,
    clients=(),
    observed_at=123.0,
):
    if panes is None:
        panes = {"%1": "@0", "%2": "@0", "%3": "@1"}
    return FocusInventory(
        server_identity=identity,
        panes=dict(panes),
        clients=tuple(clients),
        observed_at=observed_at,
    )


class FakeRegistry:
    def __init__(self, *participants):
        self._participants = list(participants)

    def list(self, **kwargs):
        return list(self._participants)


def participant(pid: str = "p1", pane: str | None = "%1", identity: str | None = IDENT):
    return Participant(id=pid, tmux_pane=pane, tmux_server_identity=identity, harness="pi")


class PresenceScript:
    """Programmable seams for the monitor: inventories, hooks, waiter."""

    def __init__(self, inventories):
        self.inventories = list(inventories)
        self.observe_calls = 0
        self.ensure_calls = 0
        self.installs = []
        self.removals = []
        self.wake = asyncio.Event()
        self.failure: Exception | None = None

    async def observe_focus_inventory(self):
        self.observe_calls += 1
        if self.failure is not None:
            raise self.failure
        if self.inventories:
            return self.inventories.pop(0)
        return make_inventory(clients=())

    async def ensure_focus_events(self):
        self.ensure_calls += 1
        return type("Status", (), {"previously_off": False, "focusless_clients": ()})()

    async def install_focus_wake_hooks(self, channel):
        self.installs.append(channel)
        return ["-g:client-focus-in[0]"]

    async def remove_focus_wake_hooks(self, channel):
        self.removals.append(channel)

    async def wait_for_wake(self, channel):
        await self.wake.wait()
        self.wake.clear()


def wire(monkeypatch, script):
    for name in (
        "observe_focus_inventory",
        "ensure_focus_events",
        "install_focus_wake_hooks",
        "remove_focus_wake_hooks",
        "wait_for_wake",
    ):
        monkeypatch.setattr(tmux_presence, name, getattr(script, name))


@pytest.fixture
def one_participant():
    return FakeRegistry(participant())


# ---- derivation -------------------------------------------------------


async def test_initial_unknown_before_any_observation(monkeypatch, one_participant):
    wire(monkeypatch, PresenceScript([]))
    monitor = PresenceMonitor(one_participant)
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "not-observed"
    assert snapshot.observed_at is None
    assert snapshot.protected is True
    assert monitor.snapshot("nobody").reason == "unregistered"


async def test_present_when_focused_client_has_pane_selected(monkeypatch, one_participant):
    script = PresenceScript([make_inventory(clients=[make_client()])])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    assert monitor.snapshot("p1").reason == "focused-viewer"


async def test_blur_releases_then_refocus_protects(monkeypatch, one_participant):
    script = PresenceScript(
        [
            make_inventory(clients=[make_client()]),
            make_inventory(clients=[make_client(focused=False)]),
            make_inventory(clients=[make_client()]),
        ]
    )
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    assert monitor.snapshot("p1").reason == "no-focused-viewer"
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT


async def test_detach_releases(monkeypatch, one_participant):
    script = PresenceScript([make_inventory(clients=[make_client()]), make_inventory()])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT


async def test_pane_change_releases_old_pane(monkeypatch, one_participant):
    script = PresenceScript(
        [
            make_inventory(clients=[make_client(active_pane_id="%1")]),
            make_inventory(clients=[make_client(active_pane_id="%2")]),
        ]
    )
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.UNKNOWN
    assert monitor.snapshot("p1").reason == "independent-active-pane"


async def test_multi_pane_window_without_selection_is_unknown(monkeypatch):
    registry = FakeRegistry(participant("p1", "%1"), participant("p2", "%2"))
    script = PresenceScript([make_inventory(clients=[make_client(active_pane_id="%1")])])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(registry)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    assert monitor.snapshot("p2").state is PresenceState.UNKNOWN
    assert monitor.snapshot("p2").protected is True


async def test_readonly_and_control_clients_are_ignored(monkeypatch, one_participant):
    script = PresenceScript(
        [
            make_inventory(
                clients=[
                    make_client(readonly=True),
                    make_client(control=True, tty="/dev/ttys002", pid="502"),
                ]
            )
        ]
    )
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT


async def test_any_focused_human_wins(monkeypatch):
    registry = FakeRegistry(participant("p1", "%1"), participant("p2", "%3"))
    script = PresenceScript(
        [
            make_inventory(
                panes={"%1": "@0", "%3": "@1"},
                clients=[
                    make_client(focused=False, window_id="@0", active_pane_id="%1"),
                    make_client(window_id="@1", active_pane_id="%3", tty="/dev/ttys003", pid="503"),
                ],
            )
        ]
    )
    wire(monkeypatch, script)
    monitor = PresenceMonitor(registry)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    assert monitor.snapshot("p2").state is PresenceState.PRESENT


# ---- fail-closed ------------------------------------------------------


async def test_query_error_fails_closed_to_unknown(monkeypatch, one_participant):
    script = PresenceScript([])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    script.failure = RuntimeError("tmux exploded")
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason.startswith("query-failed")
    assert snapshot.observed_at is None
    script.failure = None
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT


async def test_unstamped_participant_is_unknown(monkeypatch):
    registry = FakeRegistry(participant(identity=None))
    wire(monkeypatch, PresenceScript([make_inventory()]))
    monitor = PresenceMonitor(registry)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "identity-unstamped"


async def test_server_restart_fails_closed(monkeypatch, one_participant):
    script = PresenceScript(
        [make_inventory(), make_inventory(identity=OTHER_IDENT, clients=[make_client()])]
    )
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "server-identity-changed"


async def test_pane_missing_from_inventory_is_unknown(monkeypatch, one_participant):
    script = PresenceScript([make_inventory(panes={"%9": "@0"})])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "pane-not-in-inventory"


async def test_paneless_participant_is_absent(monkeypatch):
    registry = FakeRegistry(participant(pane=None))
    wire(monkeypatch, PresenceScript([make_inventory()]))
    monitor = PresenceMonitor(registry)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    assert monitor.snapshot("p1").reason == "no-pane"


# ---- require_absent ----------------------------------------------------


async def test_require_absent_refuses_present_and_unknown(monkeypatch, one_participant):
    script = PresenceScript(
        [make_inventory(clients=[make_client()]), make_inventory(panes={"%9": "@0"})]
    )
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    with pytest.raises(HumanPresent):
        await monitor.require_absent("p1")
    with pytest.raises(Busy):
        await monitor.require_absent("p1")
    script.inventories.append(make_inventory())
    await monitor.require_absent("p1")


async def test_require_absent_always_takes_a_fresh_inventory(monkeypatch, one_participant):
    script = PresenceScript([make_inventory(clients=[make_client()]), make_inventory()])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    # The cached PRESENT must not satisfy a control's absence requirement.
    await monitor.require_absent("p1")
    assert script.observe_calls == 2


# ---- coalescing and wakeups -------------------------------------------


async def test_refresh_coalesces_concurrent_callers(monkeypatch, one_participant):
    script = PresenceScript([])
    wire(monkeypatch, script)

    async def slow_inventory():
        script.observe_calls += 1
        await asyncio.sleep(0.05)
        return make_inventory()

    monkeypatch.setattr(tmux_presence, "observe_focus_inventory", slow_inventory)
    monitor = PresenceMonitor(one_participant)
    await asyncio.gather(*(monitor.refresh() for _ in range(5)))
    assert script.observe_calls == 1
    assert monitor.revision == 1


async def test_wait_for_change_has_no_missed_wakeups(monkeypatch, one_participant):
    wire(monkeypatch, PresenceScript([]))
    monitor = PresenceMonitor(one_participant)
    waiter = asyncio.create_task(monitor.wait_for_change(0))
    await asyncio.sleep(0)
    await monitor.refresh()
    assert await asyncio.wait_for(waiter, 1.0) == 1
    # A revision already published resolves immediately.
    assert await asyncio.wait_for(monitor.wait_for_change(0), 1.0) == 1


async def test_multiple_waiters_all_wake_on_one_publish(monkeypatch, one_participant):
    wire(monkeypatch, PresenceScript([]))
    monitor = PresenceMonitor(one_participant)
    waiters = [asyncio.create_task(monitor.wait_for_change(0)) for _ in range(3)]
    await asyncio.sleep(0)
    await monitor.refresh()
    revisions = await asyncio.wait_for(asyncio.gather(*waiters), 1.0)
    assert revisions == [1, 1, 1]


async def test_waiter_wake_triggers_a_fresh_snapshot(monkeypatch, one_participant):
    script = PresenceScript([make_inventory(clients=[make_client()]), make_inventory()])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant, refresh_interval=60.0)
    await monitor.start()
    try:
        assert monitor.snapshot("p1").state is PresenceState.PRESENT
        # The hook waiter returns: the wake must produce a second observation.
        script.wake.set()
        for _ in range(100):
            if script.observe_calls >= 2:
                break
            await asyncio.sleep(0.01)
        assert monitor.snapshot("p1").state is PresenceState.ABSENT
    finally:
        await monitor.aclose()


async def test_periodic_refresh_runs_without_wakes(monkeypatch, one_participant):
    script = PresenceScript([make_inventory(), make_inventory(clients=[make_client()])])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant, refresh_interval=0.01)
    await monitor.start()
    try:
        for _ in range(200):
            if monitor.snapshot("p1").state is PresenceState.PRESENT:
                break
            await asyncio.sleep(0.01)
        assert monitor.snapshot("p1").state is PresenceState.PRESENT
    finally:
        await monitor.aclose()


# ---- lifecycle --------------------------------------------------------


async def test_start_arms_and_closes_sweeps_owned_hooks(monkeypatch, one_participant):
    script = PresenceScript([])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.start()
    assert script.ensure_calls == 1
    assert script.installs == [PRESENCE_WAKE_CHANNEL]
    await monitor.aclose()
    assert script.removals == [PRESENCE_WAKE_CHANNEL]
    assert monitor._loop_task is None and monitor._waiter_task is None


async def test_reconcile_rearms_and_refreshes(monkeypatch, one_participant):
    script = PresenceScript([])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.reconcile()
    assert script.ensure_calls == 1
    assert script.observe_calls == 1
    await monitor.start()
    # start() must not re-arm an already-armed monitor.
    assert script.ensure_calls == 1
    await monitor.reconcile()
    assert script.ensure_calls == 2
    await monitor.aclose()


async def test_aclose_is_bounded_and_cancellable_without_start(monkeypatch):
    wire(monkeypatch, PresenceScript([]))
    monitor = PresenceMonitor(FakeRegistry())
    await asyncio.wait_for(monitor.aclose(), 1.0)


async def test_start_is_idempotent(monkeypatch, one_participant):
    script = PresenceScript([])
    wire(monkeypatch, script)
    monitor = PresenceMonitor(one_participant)
    await monitor.start()
    loop_task, waiter_task = monitor._loop_task, monitor._waiter_task
    await monitor.start()
    assert monitor._loop_task is loop_task
    assert monitor._waiter_task is waiter_task
    await monitor.aclose()
