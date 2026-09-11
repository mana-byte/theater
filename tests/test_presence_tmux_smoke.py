"""Presence against a real tmux server with enforced explicit private routing."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests._presence_tmux import AttachedClient, PrivateServer
from theater.constants.presence import (
    PRESENCE_FOCUS_EVENTS_OPTION,
    PRESENCE_WAKE_CHANNEL,
)
from theater.daemon.presence import PresenceMonitor
from theater.daemon.presence.contracts import PresenceState
from theater.models import Participant
from theater.tmux import client
from theater.tmux.command import TmuxError
from theater.tmux.presence import (
    ensure_focus_events,
    install_focus_wake_hooks,
    observe_focus_inventory,
    remove_focus_wake_hooks,
    wait_for_wake,
    wake_command,
)

pytestmark = pytest.mark.tmux

SESSION = "theater-presence-smoke"
CHANNEL = PRESENCE_WAKE_CHANNEL


@pytest.fixture
def tmux_server(monkeypatch, private_tmux_socket):
    if not client.available():
        pytest.skip("tmux is not on PATH")
    root = Path(tempfile.mkdtemp(prefix="thr-presence-", dir="/tmp"))
    server = PrivateServer(root)
    run, create = subprocess.run, asyncio.create_subprocess_exec

    def routed_run(args, **kwargs):
        return run(server.argv(args), **kwargs)

    async def routed_create(*args, **kwargs):
        proc = await create(*server.argv(args), **kwargs)
        if args[0] == "tmux" and "wait-for" in args:
            server.waiters.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "run", routed_run)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", routed_create)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    client.run_sync("-f", "/dev/null", "new-session", "-d", "-s", SESSION, "-x", "120", "-y", "30")
    try:
        yield server
    finally:
        socket = client.run_sync("display-message", "-p", "#{socket_path}")
        assert Path(socket).resolve() == Path(server.socket), "private server identity changed"
        client.run_sync("kill-server")
        shutil.rmtree(root)


async def _async_tmux(server, *args):
    return await server.command(*args)


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached within timeout")


class RegistryStub:
    def __init__(self, *participants):
        self._participants = list(participants)

    def list(self, **kwargs):
        return list(self._participants)

    def get(self, participant_id):
        return next((p for p in self._participants if p.id == participant_id), None)


def _make_monitor(inventory, pane_id):
    registry = RegistryStub(
        Participant(
            id="smoke",
            harness="pi",
            tmux_pane=pane_id,
            tmux_server_identity=inventory.server_identity,
        )
    )
    return PresenceMonitor(registry, refresh_interval=60.0)


async def test_ensure_focus_events_flips_on_and_diagnoses(tmux_server):
    root = tmux_server
    before = await _async_tmux(root, "show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION)
    status = await ensure_focus_events()
    assert status.previously_off is (before == "off")
    after = await _async_tmux(root, "show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION)
    assert after == "on"


async def test_hooks_preserve_user_entries_and_stay_swept(tmux_server):
    root = tmux_server
    await ensure_focus_events()
    await _async_tmux(root, "set-hook", "-g", "client-focus-in[0]", "run-shell true")
    await _async_tmux(
        root, "set-hook", "-t", SESSION + ":", "client-focus-in[0]", "run-shell false"
    )
    await install_focus_wake_hooks(CHANNEL)
    await install_focus_wake_hooks(CHANNEL)
    ours = wake_command(CHANNEL)
    hooks = await _async_tmux(root, "show-hooks", "-g", "client-focus-in")
    assert "run-shell true" in hooks
    assert ours in hooks
    local = await _async_tmux(root, "show-hooks", "-t", SESSION + ":", "client-focus-in")
    assert "run-shell false" in local and local.count(ours) == 1
    await remove_focus_wake_hooks(CHANNEL)
    hooks = await _async_tmux(root, "show-hooks", "-g", "client-focus-in")
    assert "run-shell true" in hooks
    assert ours not in hooks
    local = await _async_tmux(root, "show-hooks", "-t", SESSION + ":", "client-focus-in")
    assert "run-shell false" in local and ours not in local
    # Shutdown never disables focus reporting.
    after = await _async_tmux(root, "show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION)
    assert after == "on"


async def test_same_window_selection_releases_and_restores(tmux_server):
    """The regression: selecting another pane of the window releases ours."""
    root = tmux_server
    await ensure_focus_events()
    await install_focus_wake_hooks(CHANNEL)
    await _async_tmux(root, "split-window", "-d", "-t", SESSION)
    inventory = await observe_focus_inventory()
    same_window = [p for p, w in inventory.panes.items() if w == "@0"]
    pane_id, other_pane = sorted(same_window)
    monitor = _make_monitor(inventory, pane_id)
    await monitor.start()
    attached = AttachedClient.spawn(root, SESSION)
    try:
        state = lambda: monitor.snapshot("smoke").state  # noqa: E731
        await _until(lambda: state() is PresenceState.PRESENT)
        snapshot = monitor.snapshot("smoke")
        assert snapshot.reason == "focused-viewer"

        # A regular pane selection inside the same window releases the pane.
        await _async_tmux(root, "select-pane", "-t", other_pane)
        await _until(lambda: state() is PresenceState.ABSENT)
        snapshot = monitor.snapshot("smoke")
        assert snapshot.reason == "pane-released"

        await _async_tmux(root, "select-pane", "-t", pane_id)
        await _until(lambda: state() is PresenceState.PRESENT)
    finally:
        attached.close()
        await monitor.aclose()


async def test_waiter_wakes_on_focus_hooks_and_no_orphan_remains(tmux_server):
    root = tmux_server
    await ensure_focus_events()
    await install_focus_wake_hooks(CHANNEL)
    inventory = await observe_focus_inventory()
    pane_id = next(iter(inventory.panes))
    monitor = _make_monitor(inventory, pane_id)
    await monitor.start()
    attached = AttachedClient.spawn(root, SESSION)
    try:
        state = lambda: monitor.snapshot("smoke").state  # noqa: E731
        # The attach hook wakes the monitor: the focused client is present.
        await _until(lambda: state() is PresenceState.PRESENT)
        assert monitor.snapshot("smoke").state is PresenceState.PRESENT

        # Blur through the real terminal: the hook wakes a fresh inventory.
        attached.write(b"\x1b[O")
        await _until(lambda: state() is PresenceState.ABSENT)

        attached.write(b"\x1b[I")
        await _until(lambda: state() is PresenceState.PRESENT)
    finally:
        attached.close()
        await monitor.aclose()
    # No orphan tmux wait-for client survives shutdown.
    assert tmux_server.waiters
    assert all(proc.returncode is not None for proc in tmux_server.waiters)
    # The daemon never detaches clients or disables focus events.
    after = await _async_tmux(root, "show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION)
    assert after == "on"


async def test_wait_for_wake_rejects_nonzero_exit(tmux_server):
    """A tmux that cannot be reached must raise, not hang or pretend."""
    tmux_server.socket = str(tmux_server.root / "missing.sock")
    try:
        with pytest.raises(TmuxError):
            await asyncio.wait_for(wait_for_wake(CHANNEL), 5)
    finally:
        tmux_server.socket = str(tmux_server.root / "server.sock")


async def test_wait_for_wake_cancellation_reaps_the_client(tmux_server):
    task = asyncio.create_task(wait_for_wake(CHANNEL))
    try:
        await _until(lambda: bool(tmux_server.waiters))
        proc = tmux_server.waiters[-1]
        assert proc.returncode is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert proc.returncode is not None


async def test_readonly_client_does_not_protect_input(tmux_server):
    attached = AttachedClient.spawn(tmux_server, SESSION, "-r")
    try:
        for _ in range(100):
            inventory = await observe_focus_inventory()
            if inventory.clients:
                break
            await asyncio.sleep(0.05)
        assert inventory.clients and all(not c.input_capable for c in inventory.clients)
        monitor = _make_monitor(inventory, next(iter(inventory.panes)))
        await monitor.refresh()
        assert monitor.snapshot("smoke").state is PresenceState.ABSENT
    finally:
        attached.close()


async def test_independent_input_pane_protects_window_until_trusted_blur(tmux_server):
    await tmux_server.command("split-window", "-d", "-t", SESSION)
    inventory = await observe_focus_inventory()
    monitor = _make_monitor(inventory, sorted(inventory.panes)[1])
    await monitor.start()
    attached = AttachedClient.spawn(tmux_server, SESSION, "-f", "active-pane")
    try:
        await _until(lambda: monitor.snapshot("smoke").reason == "independent-active-pane")
        assert monitor.snapshot("smoke").protected
        attached.write(b"\x1b[O")
        await _until(lambda: monitor.snapshot("smoke").state is PresenceState.ABSENT)
    finally:
        attached.close()
        await monitor.aclose()


async def test_control_client_does_not_protect_input(tmux_server):
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "-C",
        "attach",
        "-t",
        SESSION,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            inventory = await observe_focus_inventory()
            if inventory.clients:
                break
            await asyncio.sleep(0.05)
        assert inventory.clients and all(c.control for c in inventory.clients)
        monitor = _make_monitor(inventory, next(iter(inventory.panes)))
        await monitor.refresh()
        assert monitor.snapshot("smoke").state is PresenceState.ABSENT
    finally:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), 5)
