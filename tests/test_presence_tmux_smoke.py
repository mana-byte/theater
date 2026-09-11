"""Presence against a real private tmux server: focus flags, hooks, waiter.

Isolation is a throwaway server under its own TMUX_TMPDIR root, like
`test_tmux_rig.py` — never the developer's own server. Marked `tmux`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import pty
import shutil
import signal
import tempfile
import time

import pytest

from theater.constants.presence import (
    PRESENCE_FOCUS_EVENTS_OPTION,
    PRESENCE_WAKE_CHANNEL,
)
from theater.daemon.presence import PresenceMonitor
from theater.daemon.presence.contracts import PresenceState
from theater.models import Participant
from theater.tmux import client
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


@pytest.fixture(scope="module")
def tmux_server():
    if not client.available():
        pytest.skip("tmux is not on PATH")
    root = tempfile.mkdtemp(prefix="thr-presence", dir="/tmp")
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TMUX_TMPDIR", root)
        mp.setenv("TERM", "xterm-256color")
        mp.delenv("TMUX", raising=False)
        mp.delenv("TMUX_PANE", raising=False)
        client.run_sync("new-session", "-d", "-s", SESSION, "-x", "120", "-y", "30")
        try:
            yield root
        finally:
            client.run_sync("kill-server", check=False)
    shutil.rmtree(root, ignore_errors=True)


def _tmux_env(root):
    env = dict(os.environ, TMUX_TMPDIR=root, TERM="xterm-256color")
    env.pop("TMUX", None)
    return env


async def _async_tmux(root, *args):
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_tmux_env(root),
    )
    out, _ = await asyncio.wait_for(proc.communicate(), 10.0)
    return out.decode().strip()


async def _pgrep_channel() -> str:
    proc = await asyncio.create_subprocess_exec(
        "pgrep", "-f", f"wait-for {CHANNEL}", stdout=asyncio.subprocess.PIPE
    )
    out, _ = await proc.communicate()
    return out.decode().strip()


async def _until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached within timeout")


class AttachedClient:
    """A real tmux client behind a pty the test can write escapes into."""

    def __init__(self, root, pid, fd):
        self.root = root
        self.pid = pid
        self.fd = fd

    @classmethod
    def spawn(cls, root) -> AttachedClient:
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe("tmux", ["tmux", "attach", "-t", SESSION], _tmux_env(root))
            os._exit(1)
        return cls(root, pid, fd)

    def write(self, data: bytes) -> None:
        os.write(self.fd, data)

    def kill(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.kill(self.pid, signal.SIGKILL)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(self.pid, 0)


class RegistryStub:
    def __init__(self, *participants):
        self._participants = list(participants)

    def list(self, **kwargs):
        return list(self._participants)


async def test_ensure_focus_events_flips_on_and_diagnoses(tmux_server):
    root = tmux_server
    before = await _async_tmux(root, "show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION)
    status = await ensure_focus_events()
    assert status.previously_off is (before == "off")
    after = await _async_tmux(root, "show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION)
    assert after == "on"


async def test_hooks_preserve_user_entries_and_stay_swept(tmux_server):
    root = tmux_server
    await _async_tmux(root, "set-hook", "-g", "client-focus-in[0]", "run-shell true")
    await install_focus_wake_hooks(CHANNEL)
    ours = wake_command(CHANNEL)
    hooks = await _async_tmux(root, "show-hooks", "-g", "client-focus-in")
    assert "run-shell true" in hooks
    assert ours in hooks
    await remove_focus_wake_hooks(CHANNEL)
    hooks = await _async_tmux(root, "show-hooks", "-g", "client-focus-in")
    assert "run-shell true" in hooks
    assert ours not in hooks
    # Shutdown never disables focus reporting.
    after = await _async_tmux(root, "show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION)
    assert after == "on"


async def test_waiter_wakes_on_focus_hooks_and_no_orphan_remains(tmux_server):
    root = tmux_server
    await ensure_focus_events()
    await install_focus_wake_hooks(CHANNEL)
    inventory = await observe_focus_inventory()
    pane_id = next(iter(inventory.panes))
    registry = RegistryStub(
        Participant(
            id="smoke",
            harness="pi",
            tmux_pane=pane_id,
            tmux_server_identity=inventory.server_identity,
        )
    )
    monitor = PresenceMonitor(registry, refresh_interval=60.0)
    await monitor.start()
    attached = AttachedClient.spawn(root)
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
        attached.kill()
        await monitor.aclose()
    # No orphan tmux wait-for client survives shutdown.
    assert await _pgrep_channel() == ""
    # The daemon never detaches clients or disables focus events.
    after = await _async_tmux(root, "show-options", "-g", "-v", PRESENCE_FOCUS_EVENTS_OPTION)
    assert after == "on"


async def test_wait_for_wake_cancellation_reaps_the_client(tmux_server):
    task = asyncio.create_task(wait_for_wake(CHANNEL))
    await asyncio.sleep(0.3)
    assert await _pgrep_channel() != "", "wait-for client should be running while awaited"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _pgrep_channel() == "", "cancelled waiter left an orphan client"
