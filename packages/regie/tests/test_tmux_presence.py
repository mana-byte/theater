"""RC9 presence parity and focus-wake fencing without live terminal input."""

from __future__ import annotations

import asyncio
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
