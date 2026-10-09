"""RC9 presence parity and focus-wake fencing without live terminal input."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest
from regie.tmux.command import TmuxError
from regie.tmux.focus_facts import FocusClient, FocusInventory, FocusPane, parse_clients
from regie.tmux.focus_hooks import FocusHooks
from regie.tmux.focus_monitor import FocusMonitor
from regie.tmux.focus_policy import FocusTrust, classify
from regie.tmux.identity import PaneSnapshot, ServerIdentity

_SERVER = ServerIdentity("/test/tmux", "11", "22").value


def _pane() -> PaneSnapshot:
    return PaneSnapshot(
        server_identity=_SERVER,
        pane_id="%7",
        pane_pid=42,
        dead=False,
        executable="agent",
        window_id="@1",
        provider_id="provider-a",
        terminal_incarnation="incarnation-a",
        occupant_id="participant-a",
        occupant_digest="digest",
        occupant_pane_pid=42,
        launch_id="launch-a",
        launch_executable="agent",
    )


def _client(**changes) -> FocusClient:
    return replace(
        FocusClient(
            ("tty", "pid", "created", "session", "session-created"),
            frozenset({"focused"}),
            False,
            False,
            "@1",
            "%7",
            frozenset({"focus"}),
        ),
        **changes,
    )


def _facts(*clients, enabled=True) -> FocusInventory:
    return FocusInventory(
        _SERVER,
        {"%7": FocusPane("@1", 42, "copy"), "%8": FocusPane("@1", 99, None)},
        clients,
        enabled,
    )


@pytest.mark.parametrize(
    ("clients", "state", "reason"),
    [
        ((_client(),), "present", "focused_viewer"),
        ((_client(features=frozenset()),), "present", "focused_viewer"),
        ((_client(pane_id="%8", flags=frozenset()),), "absent", "pane_released"),
        ((_client(pane_id=""),), "unknown", "selection_unobservable"),
        ((_client(pane_id="%missing"),), "unknown", "selection_unobservable"),
        (
            (_client(flags=frozenset({"focused", "active-pane"})),),
            "unknown",
            "independent_active_pane",
        ),
        ((_client(readonly=True), _client(control=True)), "absent", "no_input_capable_viewer"),
        ((_client(), _client(flags=frozenset())), "present", "focused_viewer"),
    ],
)
def test_focus_policy_parity(clients, state, reason):
    result = classify(_pane(), _facts(*clients), FocusTrust())
    assert (result.state, result.reason, result.mode) == (state, reason, "copy")


def test_blur_trust_is_lifetime_scoped_and_requires_reporting():
    trust = FocusTrust()
    trust.armed = True
    focused = _client()
    blurred = _client(flags=frozenset())
    assert classify(_pane(), _facts(blurred), trust).state == "unknown"
    trust.observe((focused,))
    trust.observe((blurred,))
    assert classify(_pane(), _facts(blurred), trust).state == "absent"
    changed = replace(blurred, identity=("new-tty", *blurred.identity[1:]))
    trust.observe((changed,))
    assert classify(_pane(), _facts(changed), trust).state == "unknown"
    trust.observe((focused,))
    unsupported = replace(blurred, features=frozenset())
    trust.observe((unsupported,))
    assert classify(_pane(), _facts(unsupported), trust).state == "unknown"
    trust.observe((focused,))
    trust.observe((blurred,))
    trust.armed = False
    assert classify(_pane(), _facts(blurred, enabled=False), trust).state == "unknown"
    trust.invalidate()
    trust.armed = True
    trust.observe((blurred,))
    assert classify(_pane(), _facts(blurred), trust).state == "unknown"
    trust.observe(())
    assert classify(_pane(), _facts(), trust).state == "absent"


def test_focus_client_identity_and_unknown_selection_are_not_conflated():
    from regie.tmux.command import TmuxError

    client = parse_clients("tty\t123\t456\t$1\t789\tfocused\t0\t0\t@1\t\tfocus")[0]
    assert client.identity == ("tty", "123", "456", "$1", "789")
    assert client.pane_id == ""
    with pytest.raises(TmuxError):
        parse_clients("\t123\t456\t$1\t789\tfocused\t0\t0\t@1\t%7\tfocus")


async def test_focus_hook_inventory_and_installation_are_batched_per_scope(monkeypatch):
    calls: list[tuple[str, ...]] = []

    async def run(_self, *args: str, **_kwargs: object) -> str:
        calls.append(args)
        if args[0] == "display-message":
            return "/test/tmux\t11\t22"
        if args[0] == "show-options":
            return "on"
        if args[0] == "list-sessions":
            return "session\t$1\nwindow\t@1"
        if args[0] in {"show-hooks", "set-hook"}:
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(FocusHooks, "_run", run)

    assert await FocusHooks(_SERVER).arm()
    scope_reads = [args for args in calls if args[0] == "list-sessions"]
    installs = [args for args in calls if args[0] == "set-hook"]
    assert len(scope_reads) == 1
    assert "list-windows" in scope_reads[0]
    assert len(installs) == 2
    assert all(args.count("set-hook") > 1 for args in installs)


async def test_focus_wake_discards_inflight_absence_but_preserves_transition_trust(monkeypatch):
    wakes = asyncio.Queue()
    reading = asyncio.Event()
    release_old = asyncio.Event()
    release_fresh = asyncio.Event()
    reads = 0
    closed = []

    class Hooks:
        def __init__(self, identity):
            self.identity = ServerIdentity.parse(identity)

        async def arm(self):
            return True

        async def wait(self):
            await wakes.get()

        async def close(self):
            closed.append(True)

    async def read(_identity):
        nonlocal reads
        reads += 1
        if reads <= 2:
            return _facts(_client())
        if reads == 3:
            reading.set()
            await release_old.wait()
            return _facts()
        await release_fresh.wait()
        return _facts(_client(flags=frozenset()))

    monkeypatch.setattr("regie.tmux.focus_monitor.FocusHooks", Hooks)
    monkeypatch.setattr("regie.tmux.focus_monitor.read_inventory", read)
    monitor = FocusMonitor()
    await monitor.start(_SERVER)
    evidence = await monitor.observe(_pane())
    monitor.validate(evidence)
    monitor.changed.clear()
    pending = asyncio.create_task(monitor.observe(_pane()))
    try:
        await asyncio.wait_for(reading.wait(), 1)
        wakes.put_nowait(None)
        await asyncio.wait_for(monitor.changed.wait(), 1)
        with pytest.raises(TmuxError, match="focus evidence changed"):
            monitor.validate(evidence)
        release_old.set()
        assert (await pending).state == "unknown"
        assert monitor._facts is None
        release_fresh.set()
        assert (await monitor.observe(_pane())).state == "absent"
    finally:
        release_old.set()
        release_fresh.set()
        await monitor.aclose()
        await asyncio.gather(pending, return_exceptions=True)
    assert closed == [True]
    assert monitor._waiter_task is monitor._loop_task is None


async def test_fresh_read_does_not_queue_behind_an_older_read(monkeypatch):
    class Hooks:
        def __init__(self, identity):
            self.identity = ServerIdentity.parse(identity)

        async def arm(self):
            return True

        async def wait(self):
            await asyncio.Event().wait()

        async def close(self):
            pass

    old_started, release_old = asyncio.Event(), asyncio.Event()
    reads = 0

    async def read(_identity):
        nonlocal reads
        reads += 1
        if reads == 2:
            old_started.set()
            await release_old.wait()
            return _facts(_client())  # stale: still focused
        return _facts(_client(flags=frozenset()) if reads > 2 else _client())

    monkeypatch.setattr("regie.tmux.focus_monitor.FocusHooks", Hooks)
    monkeypatch.setattr("regie.tmux.focus_monitor.read_inventory", read)
    monitor = FocusMonitor()
    await monitor.start(_SERVER)
    older = asyncio.create_task(monitor.refresh())
    try:
        await asyncio.wait_for(old_started.wait(), 1)
        # The fresh observation completes while the older read is still blocked.
        assert (await asyncio.wait_for(monitor.observe(_pane()), 1)).state == "absent"
        release_old.set()
        await older
        assert monitor._facts == _facts(_client(flags=frozenset()))  # stale read discarded
    finally:
        release_old.set()
        await monitor.aclose()


async def test_inspections_share_one_read_begun_after_their_requests_arrived(monkeypatch):
    class Hooks:
        def __init__(self, identity):
            self.identity = ServerIdentity.parse(identity)

        async def arm(self):
            return True

        async def wait(self):
            await asyncio.Event().wait()

        async def close(self):
            pass

    reads = 0

    async def read(_identity):
        nonlocal reads
        reads += 1
        return _facts(_client(flags=frozenset()) if reads > 1 else _client())

    monkeypatch.setattr("regie.tmux.focus_monitor.FocusHooks", Hooks)
    monkeypatch.setattr("regie.tmux.focus_monitor.read_inventory", read)
    monitor = FocusMonitor()
    await monitor.start(_SERVER)
    try:
        arrived = time.monotonic()
        before = reads
        await asyncio.sleep(0.01)
        states = [(await monitor.observe(_pane(), requested_at=arrived)).state for _ in range(5)]
        assert states == ["absent"] * 5
        assert reads == before + 1  # one read answered every request that predates it

        await monitor.observe(_pane(), requested_at=time.monotonic())
        await monitor.observe(_pane())
        assert reads == before + 3  # a newer request, or none, always reads afresh

        monitor._invalidate("focus_refresh_pending")
        assert (await monitor.observe(_pane(), requested_at=arrived)).state == "absent"
        assert reads == before + 4  # an invalidated read is never shared
    finally:
        await monitor.aclose()


async def test_a_shared_read_discarded_behind_an_older_one_is_replaced_not_served_stale(
    monkeypatch,
):
    class Hooks:
        def __init__(self, identity):
            self.identity = ServerIdentity.parse(identity)

        async def arm(self):
            return True

        async def wait(self):
            await asyncio.Event().wait()

        async def close(self):
            pass

    older_started, release_older = asyncio.Event(), asyncio.Event()
    newer_started, release_newer = asyncio.Event(), asyncio.Event()
    last_started, release_last = asyncio.Event(), asyncio.Event()
    reads = 0

    async def read(_identity):
        nonlocal reads
        reads += 1
        number = reads
        if number == 2:  # begun before the request, finishes first with changed facts
            older_started.set()
            await release_older.wait()
            return _facts(_client())
        if number == 3:  # begun after the request, discarded once the older one installs
            newer_started.set()
            await release_newer.wait()
        if number == 4:  # the replacement read
            last_started.set()
            await release_last.wait()
        return _facts(_client(flags=frozenset()))

    monkeypatch.setattr("regie.tmux.focus_monitor.FocusHooks", Hooks)
    monkeypatch.setattr("regie.tmux.focus_monitor.read_inventory", read)
    monitor = FocusMonitor()
    await monitor.start(_SERVER)
    older = asyncio.create_task(monitor.refresh())
    await asyncio.wait_for(older_started.wait(), 1)
    requested_at = time.monotonic()
    pending = asyncio.create_task(monitor.observe(_pane(), requested_at=requested_at))
    try:
        await asyncio.wait_for(newer_started.wait(), 1)
        release_older.set()
        await older
        release_newer.set()
        await asyncio.wait_for(last_started.wait(), 1)
        assert not pending.done()  # it will not answer from the older read's facts
        release_last.set()
        await asyncio.wait_for(pending, 1)
        assert monitor._facts_started_at >= requested_at
    finally:
        release_older.set()
        release_newer.set()
        release_last.set()
        await monitor.aclose()
        await asyncio.gather(older, pending, return_exceptions=True)


@pytest.mark.parametrize("query_fails", [False, True])
async def test_reporting_failure_cannot_create_a_refresh_or_notification_loop(
    monkeypatch, query_fails
):
    class Hooks:
        def __init__(self, identity):
            self.identity = ServerIdentity.parse(identity)

        async def arm(self):
            raise TmuxError("server refused focus-events")

        async def wait(self):
            await asyncio.Event().wait()

        async def close(self):
            pass

    async def read(_identity):
        if query_fails:
            raise TmuxError("focus query failed")
        return _facts(_client(flags=frozenset()), enabled=False)

    monkeypatch.setattr("regie.tmux.focus_monitor.FocusHooks", Hooks)
    monkeypatch.setattr("regie.tmux.focus_monitor.read_inventory", read)
    monitor = FocusMonitor()
    try:
        await monitor.start(_SERVER)
        monitor.changed.clear()
        evidence = await monitor.observe(_pane())
        assert evidence.state == "unknown"
        assert not monitor.changed.is_set()
        assert not monitor._wake.is_set()
        assert monitor._armed_at > 0
    finally:
        await monitor.aclose()


async def _scoped_monitor(monkeypatch, reads):
    class Hooks:
        def __init__(self, identity):
            self.identity = ServerIdentity.parse(identity)

        async def arm(self):
            return True

        async def wait(self):
            await asyncio.Event().wait()

        async def close(self):
            pass

    async def read(_identity):
        return reads.pop(0)

    monkeypatch.setattr("regie.tmux.focus_monitor.FocusHooks", Hooks)
    monkeypatch.setattr("regie.tmux.focus_monitor.read_inventory", read)
    monitor = FocusMonitor()
    await monitor.start(_SERVER)
    monitor.take_changed_panes()
    return monitor


async def test_install_scopes_changes_to_the_pane_whose_focus_state_differs(monkeypatch):
    other = FocusClient(
        ("tty2", "pid", "created", "session", "session-created"),
        frozenset({"focused"}),
        False,
        False,
        "@2",
        "%8",
        frozenset({"focus"}),
    )
    first = replace(
        _facts(_client(), other), panes={**_facts().panes, "%8": FocusPane("@2", 99, None)}
    )
    second = replace(first, clients=(_client(flags=frozenset()), other))
    monitor = await _scoped_monitor(monkeypatch, [first, first, second, second])
    try:
        await monitor.refresh()
        assert monitor.take_changed_panes() == frozenset()  # same facts: nothing moved
        monitor.changed.clear()
        await monitor.refresh(fresh=True)
        assert monitor.changed.is_set()
        assert monitor.take_changed_panes() == frozenset({"%7"})
        await monitor.refresh(fresh=True)
        assert monitor.take_changed_panes() == frozenset()  # a taken scope is never reused
    finally:
        await monitor.aclose()


async def test_unattributable_changes_are_wholesale_and_unrelated_ones_empty(monkeypatch):
    facts = _facts(_client())
    unrelated = replace(facts, clients=(*facts.clients, _client(readonly=True)))
    monitor = await _scoped_monitor(monkeypatch, [facts, unrelated, unrelated, unrelated])
    try:
        await monitor.refresh()
        assert monitor.take_changed_panes() == frozenset()  # only a read-only client appeared
        assert monitor.changed.is_set()
        monitor._invalidate("focus_refresh_pending")  # wake: facts dropped wholesale
        assert monitor.take_changed_panes() is None
        assert monitor.take_changed_panes() == frozenset()
        await monitor.refresh(fresh=True)  # no baseline survives the drop
        assert monitor.take_changed_panes() is None
    finally:
        await monitor.aclose()
        assert monitor.take_changed_panes() is None  # monitor close is wholesale too
