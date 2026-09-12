"""Deterministic PresenceMonitor tests against scripted inventories."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

import theater.tmux.presence as tmux_presence
from theater.constants.presence import PRESENCE_WAKE_CHANNEL
from theater.daemon.presence.contracts import PresenceState
from theater.daemon.presence.monitor import PresenceMonitor
from theater.models import HumanPresent, Participant
from theater.tmux.presence import FocusClient, FocusEventsStatus, FocusInventory

IDENT = '["/tmp/sock","101","1"]'
OTHER_IDENT = '["/tmp/sock","202","2"]'
STALE_AFTER = 50.0


class Clock:
    """A mutable now the monitor trusts, so staleness is testable."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_client(
    *,
    window_id="@0",
    active_pane_id="%1",
    focused=True,
    readonly=False,
    control=False,
    tty="/dev/ttys001",
    pid="501",
    created="1789162985",
    session="main",
    session_id="$0",
    session_created="1789162980",
    termfeatures=("focus",),
    flags=(),
):
    client_flags = {"attached"} | set(flags)
    if focused:
        client_flags.add("focused")
    return FocusClient(
        tty=tty,
        pid=pid,
        created=created,
        session=session,
        session_id=session_id,
        session_created=session_created,
        flags=frozenset(client_flags),
        readonly=readonly,
        control=control,
        window_id=window_id,
        active_pane_id=active_pane_id,
        termfeatures=frozenset(termfeatures),
    )


def make_inventory(
    clock,
    *,
    identity=IDENT,
    panes=None,
    pane_pids=None,
    clients=(),
    focus_events_enabled=True,
):
    if panes is None:
        panes = {"%1": "@0", "%2": "@0", "%3": "@1"}
    if pane_pids is None:
        pane_pids = {"%1": "1001", "%2": "1002", "%3": "1003"}
    return FocusInventory(
        server_identity=identity,
        panes=dict(panes),
        pane_pids=dict(pane_pids),
        clients=tuple(clients),
        observed_at=clock.now,
        focus_events_enabled=focus_events_enabled,
    )


class FakeRegistry:
    def __init__(self, *participants):
        self._participants = list(participants)
        self.list_calls = 0

    def list(self, **kwargs):
        self.list_calls += 1
        return list(self._participants)

    def get(self, pid):
        return next((p for p in self._participants if p.id == pid), None)


def participant(
    pid: str = "p1",
    pane: str | None = "%1",
    identity: str | None = IDENT,
    pane_pid: int | None = None,
):
    return Participant(
        id=pid,
        tmux_pane=pane,
        tmux_server_identity=identity,
        pid=pane_pid,
        harness="pi",
    )


class PresenceScript:
    """Programmable seams for the monitor: inventories, hooks, waiter."""

    def __init__(self, inventories, clock):
        self.inventories = list(inventories)
        self.clock = clock
        self.observe_calls = 0
        self.ensure_calls = 0
        self.installs = []
        self.removals = []
        self.wake = asyncio.Event()
        self.failure: Exception | None = None
        self.ensure_failure: Exception | None = None
        self.previously_off = False
        self.ensure_enabled = True

    async def observe_focus_inventory(self):
        self.observe_calls += 1
        if self.failure is not None:
            raise self.failure
        if self.inventories:
            return self.inventories.pop(0)
        return make_inventory(self.clock, clients=())

    async def ensure_focus_events(self):
        self.ensure_calls += 1
        if self.ensure_failure is not None:
            raise self.ensure_failure
        return FocusEventsStatus(self.ensure_enabled, self.previously_off, ())

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


def make_monitor(registry, clock, script, **kwargs):
    return PresenceMonitor(
        registry,
        clock=clock,
        stale_after=STALE_AFTER,
        **kwargs,
    )


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def one_participant():
    registry = FakeRegistry(participant())
    registry.list_calls = 0
    return registry


# ---- derivation -------------------------------------------------------


async def test_initial_unknown_before_any_observation(monkeypatch, clock, one_participant):
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "not-observed"
    assert snapshot.observed_at is None
    assert snapshot.protected is True
    assert monitor.snapshot("nobody").reason == "unregistered"


async def test_present_when_focused_client_has_pane_selected(monkeypatch, clock, one_participant):
    script = PresenceScript([make_inventory(clock, clients=[make_client()])], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    assert monitor.snapshot("p1").reason == "focused-viewer"


async def test_first_blur_is_untrusted_until_a_transition_is_seen(
    monkeypatch, clock, one_participant
):
    """CLIENT_FOCUSED defaults on: a blur literal alone proves nothing."""
    script = PresenceScript(
        [make_inventory(clock, clients=[make_client(focused=False, termfeatures=())])],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "focus-unverified"


async def test_blur_releases_once_lifetime_evidence_exists(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, clients=[make_client(focused=False)]),
            make_inventory(clock, clients=[make_client()]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor._arm()
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    clock.advance(1)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    assert monitor.snapshot("p1").reason == "pane-released"
    clock.advance(1)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT


async def test_non_reporting_terminal_never_proves_blur(monkeypatch, clock, one_participant):
    """Probe: focused then blurred with empty termfeatures stays UNKNOWN."""
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client(termfeatures=())]),
            make_inventory(clock, clients=[make_client(focused=False, termfeatures=())]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor._arm()
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    clock.advance(1)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "focus-unverified"


async def test_blur_pair_stays_unknown_while_unarmed(monkeypatch, clock, one_participant):
    """Probe: the same focused/blur pair while arm is invalid stays UNKNOWN."""
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, clients=[make_client(focused=False)]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    clock.advance(1)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "focus-unverified"


async def test_rebound_tty_session_is_a_new_lifetime(monkeypatch, clock, one_participant):
    """Probe: same pid/created returning on another tty stays UNKNOWN."""
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, clients=[make_client(focused=False)]),
            make_inventory(clock, clients=[make_client(focused=False, tty="/dev/ttys099")]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor._arm()
    await monitor.refresh()
    clock.advance(1)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT  # trusted blur
    clock.advance(1)
    await monitor.refresh()  # same pid/created, different tty/session lifetime
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "focus-unverified"


async def test_independent_active_pane_flag_protects_whole_window(monkeypatch, clock):
    """Probe: a flagged independent selection protects every pane in @0."""
    registry = FakeRegistry(participant("p1", "%1"), participant("p2", "%2"))
    script = PresenceScript(
        [
            make_inventory(
                clock,
                clients=[make_client(active_pane_id="%2", flags=("active-pane",))],
            )
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(registry, clock, script)
    await monitor.refresh()
    for pane_owner in ("p1", "p2"):
        snapshot = monitor.snapshot(pane_owner)
        assert snapshot.state is PresenceState.UNKNOWN
        assert snapshot.protected
        assert snapshot.reason == "independent-active-pane"


async def test_trusted_blur_releases_flagged_client_too(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [
            make_inventory(
                clock, clients=[make_client(active_pane_id="%2", flags=("active-pane",))]
            ),
            make_inventory(
                clock,
                clients=[make_client(focused=False, active_pane_id="%2", flags=("active-pane",))],
            ),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor._arm()
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.UNKNOWN
    assert monitor.snapshot("p1").reason == "independent-active-pane"
    clock.advance(1)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    assert monitor.snapshot("p1").reason == "pane-released"


async def test_detach_releases(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [make_inventory(clock, clients=[make_client()]), make_inventory(clock)], clock
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    clock.advance(1)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.ABSENT
    assert snapshot.reason == "no-viewer"


async def test_same_window_selection_releases_old_pane(monkeypatch, clock, one_participant):
    """A regular client selecting another pane of the window releases ours."""
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client(active_pane_id="%1")]),
            make_inventory(clock, clients=[make_client(active_pane_id="%2")]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    clock.advance(1)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.ABSENT
    assert snapshot.reason == "pane-released"


async def test_unobservable_selection_is_unknown(monkeypatch, clock, one_participant):
    """Ordinary missing selection protects scope without claiming independence."""
    script = PresenceScript(
        [make_inventory(clock, clients=[make_client(active_pane_id="%3")])], clock
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "selection-unobservable"


async def test_readonly_and_control_clients_are_ignored(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [
            make_inventory(
                clock,
                clients=[
                    make_client(readonly=True),
                    make_client(control=True, tty="/dev/ttys002", pid="502"),
                ],
            )
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT


async def test_any_human_wins(monkeypatch, clock):
    registry = FakeRegistry(participant("p1", "%1"), participant("p2", "%3"))
    script = PresenceScript(
        [
            make_inventory(
                clock,
                panes={"%1": "@0", "%3": "@1"},
                pane_pids={"%1": "1001", "%3": "1003"},
                clients=[
                    make_client(window_id="@1", active_pane_id="%3", tty="/dev/ttys003", pid="503"),
                ],
            )
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(registry, clock, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    assert monitor.snapshot("p2").state is PresenceState.PRESENT


# ---- fail-closed ------------------------------------------------------


async def test_query_error_fails_closed_to_unknown(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    script.failure = RuntimeError("tmux exploded")
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason.startswith("query-failed")
    assert snapshot.observed_at is None
    script.failure = None
    clock.advance(1)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT


async def test_publish_failure_invalidates_trusted_blur(monkeypatch, clock, one_participant):
    """A query failure must not leave blur evidence reusable after the gap."""
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, clients=[make_client(focused=False)]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor._arm()
    await monitor.refresh()
    clock.advance(1)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT  # trusted blur
    script.failure = RuntimeError("tmux exploded")
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.UNKNOWN
    script.failure = None
    script.inventories.append(make_inventory(clock, clients=[make_client(focused=False)]))
    await monitor.refresh()  # the same blurred client cannot re-release
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "focus-unverified"


async def test_unstamped_participant_is_unknown(monkeypatch, clock):
    registry = FakeRegistry(participant(identity=None))
    wire(monkeypatch, PresenceScript([make_inventory(clock)], clock))
    monitor = make_monitor(registry, clock, None)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "identity-unstamped"


async def test_server_restart_fails_closed_and_clears_evidence(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [make_inventory(clock), make_inventory(clock, identity=OTHER_IDENT)], clock
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    clock.advance(1)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "server-identity-changed"


async def test_reused_client_identity_after_restart_stays_untrusted(
    monkeypatch, clock, one_participant
):
    """A reused client lifetime after a restart has no evidence."""
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, identity=OTHER_IDENT),
            make_inventory(clock, clients=[make_client(focused=False)]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    clock.advance(1)
    await monitor.refresh()  # restart observed, epoch bumped, evidence cleared
    clock.advance(1)
    await monitor.refresh()  # same client identity reports a blur literal
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "focus-unverified"


async def test_pane_pid_mismatch_is_unknown(monkeypatch, clock):
    registry = FakeRegistry(participant(pane_pid=1001))
    script = PresenceScript(
        [make_inventory(clock, pane_pids={"%1": "9999", "%2": "1002", "%3": "1003"})], clock
    )
    wire(monkeypatch, script)
    monitor = make_monitor(registry, clock, script)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "pane-pid-changed"


async def test_participant_binding_change_invalidates_cache(monkeypatch, clock, one_participant):
    wire(monkeypatch, PresenceScript([make_inventory(clock)], clock))
    monitor = make_monitor(one_participant, clock, None)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    # The pane moves under the participant: the cached facts are void.
    one_participant._participants[0].tmux_pane = "%2"
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "participant-changed"


async def test_pane_missing_from_inventory_is_unknown(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [make_inventory(clock, panes={"%9": "@0"}, pane_pids={"%9": "1009"})], clock
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "pane-not-in-inventory"


async def test_paneless_participant_is_absent(monkeypatch, clock):
    registry = FakeRegistry(participant(pane=None))
    wire(monkeypatch, PresenceScript([make_inventory(clock)], clock))
    monitor = make_monitor(registry, clock, None)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    assert monitor.snapshot("p1").reason == "no-pane"


# ---- staleness --------------------------------------------------------


async def test_stale_inventory_fails_closed(monkeypatch, clock, one_participant):
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    clock.advance(STALE_AFTER + 1)
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "stale-inventory"


async def test_wall_clock_jumps_never_extend_cached_facts(monkeypatch, one_participant):
    """Freshness is monotonic: a future-dated wire timestamp stinks anyway."""
    mono, wall = Clock(), Clock(start=1_000_000.0)
    script = PresenceScript([make_inventory(wall, clients=())], wall)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, mono, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    # The wire timestamp leaps far forward: freshness must not follow it.
    wall.advance(10_000.0)
    script.inventories.append(make_inventory(wall, clients=()))
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    mono.advance(STALE_AFTER + 1)
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "stale-inventory"
    assert snapshot.observed_at == 1_010_000.0


async def test_stale_cache_fails_closed_while_a_refresh_is_parked(
    monkeypatch, clock, one_participant
):
    gate = asyncio.Event()

    async def parked_observe():
        await gate.wait()
        return make_inventory(clock, clients=())

    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    monkeypatch.setattr(tmux_presence, "observe_focus_inventory", parked_observe)
    parked = asyncio.create_task(monitor.refresh())
    await asyncio.sleep(0)
    clock.advance(STALE_AFTER + 1)
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "stale-inventory"
    gate.set()
    await parked


async def test_snapshot_after_aclose_is_unknown(monkeypatch, clock, one_participant):
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    await monitor.aclose()
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "monitor-closed"


async def test_snapshot_never_scans_the_registry(monkeypatch, clock, one_participant):
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    await monitor.refresh()
    list_calls = one_participant.list_calls
    for _ in range(3):
        monitor.snapshot("p1")
    assert one_participant.list_calls == list_calls


# ---- refresh ownership -------------------------------------------------


async def test_refresh_coalesces_concurrent_callers(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)

    async def slow_inventory():
        script.observe_calls += 1
        await asyncio.sleep(0.05)
        return make_inventory(clock, clients=())

    monkeypatch.setattr(tmux_presence, "observe_focus_inventory", slow_inventory)
    monitor = make_monitor(one_participant, clock, script)
    await asyncio.gather(*(monitor.refresh() for _ in range(5)))
    assert script.observe_calls == 1
    assert monitor.revision == 1


async def test_cancelled_leader_does_not_grant_stale_absence_to_joiner(
    monkeypatch, clock, one_participant
):
    """The probe scenario: leader cancelled, joiner must not allow from cache."""
    gate = asyncio.Event()
    observe_failed = False

    async def gated_observe():
        await gate.wait()
        if observe_failed:
            raise RuntimeError("tmux exploded mid-observation")
        return make_inventory(clock, clients=())

    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    await monitor.refresh()  # establishes a fresh absent cache
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    monkeypatch.setattr(tmux_presence, "observe_focus_inventory", gated_observe)

    leader = asyncio.create_task(monitor.require_absent("p1"))
    joiner = asyncio.create_task(monitor.require_absent("p1"))
    await asyncio.sleep(0)
    # The joiner parks on the owned refresh, never on the stale cache.
    assert not joiner.done()
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    # Still parked: cancelling the leader changed nothing for the joiner.
    assert not joiner.done()
    observe_failed = True
    gate.set()
    with pytest.raises(HumanPresent):
        await joiner


async def test_joiner_allows_on_real_fresh_evidence_after_leader_cancel(
    monkeypatch, clock, one_participant
):
    gate = asyncio.Event()

    async def gated_observe():
        await gate.wait()
        return make_inventory(clock, clients=())

    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    await monitor.refresh()
    monkeypatch.setattr(tmux_presence, "observe_focus_inventory", gated_observe)
    leader = asyncio.create_task(monitor.require_absent("p1"))
    joiner = asyncio.create_task(monitor.require_absent("p1"))
    await asyncio.sleep(0)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    gate.set()
    await asyncio.wait_for(joiner, 1.0)


async def test_aclose_prevents_late_publish(monkeypatch, clock, one_participant):
    gate = asyncio.Event()

    async def gated_observe():
        await gate.wait()
        return make_inventory(clock, clients=[make_client()])

    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    await monitor.refresh()
    revision = monitor.revision
    monkeypatch.setattr(tmux_presence, "observe_focus_inventory", gated_observe)
    parked = asyncio.create_task(monitor.refresh())
    await asyncio.sleep(0)
    await monitor.aclose()
    gate.set()
    await asyncio.sleep(0.05)
    assert monitor.revision == revision
    assert monitor.snapshot("p1").reason == "monitor-closed"
    with contextlib.suppress(asyncio.CancelledError):
        await parked


# ---- require_absent ----------------------------------------------------


async def test_require_absent_refuses_present_and_unknown(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, panes={"%9": "@0"}, pane_pids={"%9": "1009"}),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    with pytest.raises(HumanPresent):
        await monitor.require_absent("p1")
    clock.advance(1)
    with pytest.raises(HumanPresent) as excinfo:
        await monitor.require_absent("p1")
    # Required-UNKNOWN refusals name the public wait, not monitor internals.
    assert "await_sessions(handles=[" in str(excinfo.value)
    assert "await_sessions(handles=['p1'])" in str(excinfo.value)
    clock.advance(1)
    script.inventories.append(make_inventory(clock))
    await monitor.require_absent("p1")


async def test_require_absent_refuses_failed_refresh_with_guidance(
    monkeypatch, clock, one_participant
):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    script.failure = RuntimeError("tmux exploded")
    with pytest.raises(HumanPresent) as excinfo:
        await monitor.require_absent("p1")
    assert "query-failed" in str(excinfo.value)
    assert "await_sessions(handles=[" in str(excinfo.value)


async def test_require_absent_settles_one_torn_inventory_read(monkeypatch, clock, one_participant):
    """A wake landing inside the inventory read is churn, not a verdict."""
    monkeypatch.setattr("theater.daemon.presence.monitor.PRESENCE_SETTLE_SECONDS", 0)
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    reads = 0

    async def torn_then_clean():
        nonlocal reads
        reads += 1
        if reads == 1:
            # The waiter processed a hook mid-read: the wake epoch moved
            # between the refresh's opening snapshot and its inventory.
            monitor._wake_epoch += 1
        return make_inventory(clock)

    monkeypatch.setattr(tmux_presence, "observe_focus_inventory", torn_then_clean)
    await monitor.require_absent("p1")
    assert reads == 2, "one torn read, one settled read"
    assert monitor.snapshot("p1").state is PresenceState.ABSENT


async def test_require_absent_refuses_when_wake_churn_never_settles(
    monkeypatch, clock, one_participant
):
    """Persistent churn exhausts the single retry and refuses fail-closed."""
    monkeypatch.setattr("theater.daemon.presence.monitor.PRESENCE_SETTLE_SECONDS", 0)
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    reads = 0

    async def always_torn():
        nonlocal reads
        reads += 1
        monitor._wake_epoch += 1
        return make_inventory(clock)

    monkeypatch.setattr(tmux_presence, "observe_focus_inventory", always_torn)
    with pytest.raises(HumanPresent) as excinfo:
        await monitor.require_absent("p1")
    assert "focus-changed-during-query" in str(excinfo.value)
    assert "await_sessions(handles=['p1'])" in str(excinfo.value)
    assert reads == 2, "exactly one retry, never more"


async def test_require_absent_does_not_retry_stable_unknowns(monkeypatch, clock, one_participant):
    """Only wake churn retries; a real UNKNOWN is a verdict, not a moment."""
    script = PresenceScript(
        [make_inventory(clock, panes={"%9": "@0"}, pane_pids={"%9": "1009"})], clock
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    with pytest.raises(HumanPresent) as excinfo:
        await monitor.require_absent("p1")
    assert "pane-not-in-inventory" in str(excinfo.value)
    assert script.observe_calls == 1, "a stable verdict refuses without a second read"


async def test_require_absent_allows_paneless_and_unregistered(monkeypatch, clock):
    registry = FakeRegistry(participant(pane=None))
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(registry, clock, None)
    await monitor.require_absent("p1")  # no pane: nothing to protect
    await monitor.require_absent("nobody")  # unregistered: metadata-safe
    assert monitor.snapshot("p1").reason == "no-pane"
    assert monitor.snapshot("p1").state is PresenceState.ABSENT


async def test_require_absent_always_takes_a_fresh_inventory(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [make_inventory(clock, clients=[make_client()]), make_inventory(clock)], clock
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.PRESENT
    # The cached PRESENT must not satisfy a control's absence requirement.
    await monitor.require_absent("p1")
    assert script.observe_calls == 2


async def test_require_absent_checks_the_option_on_admission(monkeypatch, clock, one_participant):
    """A trusted blur cannot grant absence while the option reads off."""
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, clients=[make_client(focused=False)]),
            make_inventory(clock, clients=[make_client(focused=False)]),
            make_inventory(clock, clients=[make_client(focused=False)], focus_events_enabled=False),
            make_inventory(clock, clients=[make_client(focused=False)]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor._arm()
    await monitor.refresh()  # focused
    clock.advance(1)
    await monitor.refresh()  # blurred: transition trusted
    await monitor.require_absent("p1")  # fresh admission, option verified
    clock.advance(1)
    with pytest.raises(HumanPresent) as excinfo:
        await monitor.require_absent("p1")  # fresh admission sees the option off
    assert "focus-unverified" in str(excinfo.value)
    clock.advance(1)
    with pytest.raises(HumanPresent):
        await monitor.require_absent("p1")  # invalidated evidence stays dead


# ---- arming and epochs -------------------------------------------------


async def test_option_reset_invalidates_blur_evidence(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, clients=[make_client(focused=False)]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor._arm()
    await monitor.refresh()
    clock.advance(1)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT  # transition trusted
    # Someone resets focus-events; re-arming finds it off and invalidates.
    script.previously_off = True
    await monitor._arm(force=True)  # no refresh: the cache must flip by itself
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "focus-unverified"


async def test_arm_probe_failure_invalidates_blur_evidence(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock, clients=[make_client(focused=False)]),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor._arm()
    await monitor.refresh()
    clock.advance(1)
    await monitor.refresh()
    assert monitor.snapshot("p1").state is PresenceState.ABSENT
    script.ensure_failure = RuntimeError("cannot ask tmux")
    await monitor._arm(force=True)  # failure flips the cache immediately
    assert monitor._trust.arm_ok is False
    snapshot = monitor.snapshot("p1")
    assert snapshot.state is PresenceState.UNKNOWN
    assert snapshot.reason == "focus-unverified"


async def test_unverified_option_never_becomes_arm_ok(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    script.ensure_enabled = False
    await monitor._arm(force=True)
    assert monitor._trust.arm_ok is False
    script.ensure_enabled = True
    await monitor._arm(force=True)  # retryable
    assert monitor._trust.arm_ok is True


async def test_arm_failure_is_retryable_and_flips_snapshots(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    script.ensure_failure = RuntimeError("tmux down")
    await monitor._arm(force=True)
    assert monitor._trust.arm_ok is False
    script.ensure_failure = None
    await monitor._arm(force=True)
    assert monitor._trust.arm_ok is True
    assert script.ensure_calls == 2


async def test_arm_passes_coalesce_into_one_owned_task(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await asyncio.gather(monitor.reconcile(), monitor.reconcile(), monitor.reconcile())
    assert script.ensure_calls == 1
    assert script.observe_calls == 1


async def test_aclose_cancels_an_in_flight_startup_arm(monkeypatch, clock, one_participant):
    gate = asyncio.Event()

    async def gated_ensure():
        await gate.wait()
        return FocusEventsStatus(True, False, ())

    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    monkeypatch.setattr(tmux_presence, "ensure_focus_events", gated_ensure)
    startup = asyncio.create_task(monitor.start())
    await asyncio.sleep(0)
    assert monitor._arm_task is not None and not monitor._arm_task.done()
    await monitor.aclose()
    assert monitor._arm_task is None  # owned and reaped by close
    with contextlib.suppress(asyncio.CancelledError):
        await startup
    gate.set()
    await asyncio.sleep(0.02)
    assert monitor.snapshot("p1").reason == "monitor-closed"


async def test_periodic_arm_check_retries_after_startup_failure(
    monkeypatch, clock, one_participant
):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(
        one_participant,
        clock,
        script,
        refresh_interval=0.01,
        arm_check_interval=0.05,
    )
    script.ensure_failure = RuntimeError("tmux down at boot")
    await monitor.start()
    assert monitor._trust.arm_ok is False
    assert script.ensure_calls == 1
    script.ensure_failure = None
    for _ in range(100):
        if script.ensure_calls >= 2 and monitor._trust.arm_ok:
            break
        await asyncio.sleep(0.01)
        clock.advance(0.01)
    assert monitor._trust.arm_ok is True
    assert script.ensure_calls >= 2
    await monitor.aclose()


async def test_periodic_arm_check_re_maintains_hook_coverage(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(
        one_participant,
        clock,
        script,
        refresh_interval=0.01,
        arm_check_interval=0.05,
    )
    await monitor.start()
    installs_after_start = len(script.installs)
    for _ in range(100):
        if len(script.installs) > installs_after_start:
            break
        await asyncio.sleep(0.01)
        clock.advance(0.01)
    # New sessions and option resets are re-covered periodically.
    assert len(script.installs) > installs_after_start
    await monitor.aclose()


# ---- wakeups -----------------------------------------------------------


async def test_wait_for_change_has_no_missed_wakeups(monkeypatch, clock, one_participant):
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    waiter = asyncio.create_task(monitor.wait_for_change(0))
    await asyncio.sleep(0)
    await monitor.refresh()
    assert await asyncio.wait_for(waiter, 1.0) == 1
    # A revision already published resolves immediately.
    assert await asyncio.wait_for(monitor.wait_for_change(0), 1.0) == 1


async def test_multiple_waiters_all_wake_on_one_publish(monkeypatch, clock, one_participant):
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(one_participant, clock, None)
    waiters = [asyncio.create_task(monitor.wait_for_change(0)) for _ in range(3)]
    await asyncio.sleep(0)
    await monitor.refresh()
    revisions = await asyncio.wait_for(asyncio.gather(*waiters), 1.0)
    assert revisions == [1, 1, 1]


async def test_waiter_wake_triggers_a_fresh_snapshot(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [
            make_inventory(clock, clients=[make_client()]),
            make_inventory(clock),
        ],
        clock,
    )
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script, refresh_interval=60.0)
    await monitor.start()
    try:
        assert monitor.snapshot("p1").state is PresenceState.PRESENT
        # The hook waiter returns: the wake must produce a second observation.
        script.wake.set()
        for _ in range(100):
            if script.observe_calls >= 2:
                break
            await asyncio.sleep(0.01)
            clock.advance(0.01)
        assert monitor.snapshot("p1").state is PresenceState.ABSENT
    finally:
        await monitor.aclose()


async def test_periodic_refresh_runs_without_wakes(monkeypatch, clock, one_participant):
    script = PresenceScript(
        [make_inventory(clock), make_inventory(clock, clients=[make_client()])], clock
    )
    wire(monkeypatch, script)
    monitor = make_monitor(
        one_participant, clock, script, refresh_interval=0.01, arm_check_interval=60.0
    )
    await monitor.start()
    try:
        for _ in range(200):
            if monitor.snapshot("p1").state is PresenceState.PRESENT:
                break
            await asyncio.sleep(0.01)
            clock.advance(0.01)
        assert monitor.snapshot("p1").state is PresenceState.PRESENT
    finally:
        await monitor.aclose()


# ---- lifecycle --------------------------------------------------------


async def test_start_arms_and_closes_sweeps_owned_hooks(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.start()
    assert script.ensure_calls == 1
    assert script.installs == [PRESENCE_WAKE_CHANNEL]
    await monitor.aclose()
    assert script.removals == [PRESENCE_WAKE_CHANNEL]
    assert monitor._loop_task is None and monitor._waiter_task is None


async def test_reconcile_rearms_and_refreshes(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.reconcile()
    assert script.ensure_calls == 1
    assert script.observe_calls == 1
    await monitor.start()
    # start() must not re-arm a monitor armed within the check interval.
    assert script.ensure_calls == 1
    clock.advance(60.0)
    await monitor.reconcile()
    assert script.ensure_calls == 2
    await monitor.aclose()


async def test_aclose_is_bounded_and_cancellable_without_start(monkeypatch, clock):
    wire(monkeypatch, PresenceScript([], clock))
    monitor = make_monitor(FakeRegistry(), clock, None)
    await asyncio.wait_for(monitor.aclose(), 1.0)


async def test_start_is_idempotent(monkeypatch, clock, one_participant):
    script = PresenceScript([], clock)
    wire(monkeypatch, script)
    monitor = make_monitor(one_participant, clock, script)
    await monitor.start()
    loop_task, waiter_task = monitor._loop_task, monitor._waiter_task
    await monitor.start()
    assert monitor._loop_task is loop_task
    assert monitor._waiter_task is waiter_task
    await monitor.aclose()
