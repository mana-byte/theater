"""A daemon must finish shutting down while clients are still connected.

The bug these cover: `serve()` ran the socket under `async with self._server`,
whose __aexit__ calls wait_closed(). Since 3.12 wait_closed() waits for every
connection handler to return, and Theater's handlers return only when their
client disconnects. MCP servers and the régie hold a connection open for their
whole life, so `theater stop` closed the listener and then hung forever, still
holding the lock. Clients then autostarted a replacement, which could not take
the lock — one wedged daemon blocked every future one on the machine.

Every test here keeps a live client connected, because that is the condition
that distinguished the broken code from the fixed code; without one, the old
implementation passed.
"""

from __future__ import annotations

import asyncio

from theater import paths
from theater.client import DaemonClient
from theater.daemon import lock as lock_mod
from theater.daemon.server import Daemon

#: Generous next to the sub-millisecond real cost, tight enough that the hang
#: this file exists for fails rather than stalls the suite.
BUDGET = 5.0


async def test_serve_returns_after_stop_with_a_client_attached(
    theater_home, fake_tmux
):
    daemon = Daemon(harnesses={})
    serving = asyncio.create_task(daemon.serve())
    # Wait for the listener rather than sleeping: start() is inside serve().
    while daemon._server is None:
        await asyncio.sleep(0.01)

    client = DaemonClient(autostart=False)
    await client.connect()
    try:
        daemon.stop()
        await asyncio.wait_for(serving, timeout=BUDGET)
    finally:
        await client.aclose()
        await daemon.aclose()


async def test_aclose_finishes_with_a_client_attached(theater_home, fake_tmux):
    daemon = Daemon(harnesses={})
    await daemon.start()
    client = DaemonClient(autostart=False)
    await client.connect()
    assert await client.call("ping")
    try:
        await asyncio.wait_for(daemon.aclose(), timeout=BUDGET)
    finally:
        await client.aclose()


async def test_aclose_releases_both_files_with_a_client_attached(
    theater_home, fake_tmux
):
    """The point of terminating: a successor needs the socket and the lock."""
    daemon = Daemon(harnesses={})
    await daemon.start()
    client = DaemonClient(autostart=False)
    await client.connect()
    assert await client.call("ping")
    try:
        await asyncio.wait_for(daemon.aclose(), timeout=BUDGET)
        assert not paths.socket_path().exists()
        assert not paths.pidfile_path().exists()
        assert lock_mod.is_free()
    finally:
        await client.aclose()


async def test_a_successor_can_start_immediately_after(theater_home, fake_tmux):
    """End to end: stop with a client attached, then be the daemon again."""
    first = Daemon(harnesses={})
    await first.start()
    holder = DaemonClient(autostart=False)
    await holder.connect()
    await asyncio.wait_for(first.aclose(), timeout=BUDGET)
    await holder.aclose()

    second = Daemon(harnesses={})
    await second.start()
    try:
        async with DaemonClient(autostart=False) as c:
            assert await c.call("ping")
    finally:
        await second.aclose()


async def test_run_releases_the_lock_even_if_shutdown_wedges(
    theater_home, fake_tmux, monkeypatch
):
    """A shutdown that cannot finish must not keep the lock.

    Holding it forever is the worst outcome available: no process on the
    machine can become the daemon until someone finds the pid by hand.
    """
    from theater.daemon import server as server_mod

    started = asyncio.Event()

    async def never_finishes(self):
        started.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(server_mod, "SHUTDOWN_TIMEOUT", 0.2)
    monkeypatch.setattr(Daemon, "aclose", never_finishes)

    running = asyncio.create_task(server_mod.run())
    while lock_mod.is_free():
        await asyncio.sleep(0.01)
    async with DaemonClient(autostart=False) as c:
        assert await c.call("shutdown")
    await asyncio.wait_for(running, timeout=BUDGET)

    assert lock_mod.is_free()
    assert started.is_set()


async def test_stop_reports_the_daemon_even_when_the_reply_is_lost(
    theater_home, fake_tmux, monkeypatch
):
    """A shutting-down daemon may drop the connection before the reply lands.

    `theater stop` used to treat that as "no daemon running", telling the user
    the opposite of what had just happened. Connecting is the question that
    matters; the reply is a courtesy.
    """
    from theater import cli

    daemon = Daemon(harnesses={})
    await daemon.start()
    try:
        async def lose_the_reply(self, method, **params):
            raise ConnectionError("daemon closed the connection")

        monkeypatch.setattr(DaemonClient, "call", lose_the_reply)
        assert await asyncio.to_thread(cli._shutdown_running_daemon) is True
    finally:
        await daemon.aclose()


async def test_stop_still_reports_nothing_when_no_daemon_runs(theater_home):
    from theater import cli

    assert await asyncio.to_thread(cli._shutdown_running_daemon) is False


async def test_a_daemon_whose_socket_is_deleted_stops_itself(
    theater_home, fake_tmux, monkeypatch
):
    """Unreachable is as good as dead, and it still holds the lock.

    Removing the socket file leaves the daemon listening on an inode no client
    can open. Before this, that state was terminal: clients autostarted
    replacements the lock refused, and `theater restart` could not connect to
    ask the incumbent to stop.
    """
    from theater.daemon import server as server_mod

    monkeypatch.setattr(server_mod, "REAP_INTERVAL", 0.05)
    daemon = Daemon(harnesses={})
    serving = asyncio.create_task(daemon.serve())
    while daemon._server is None:
        await asyncio.sleep(0.01)

    paths.socket_path().unlink()
    await asyncio.wait_for(serving, timeout=BUDGET)
    await daemon.aclose()
    assert lock_mod.is_free()


async def test_a_daemon_keeps_running_while_its_socket_is_there(
    theater_home, fake_tmux, monkeypatch
):
    """The other half: the check must not shoot down a healthy daemon."""
    from theater.daemon import server as server_mod

    monkeypatch.setattr(server_mod, "REAP_INTERVAL", 0.01)
    daemon = Daemon(harnesses={})
    serving = asyncio.create_task(daemon.serve())
    while daemon._server is None:
        await asyncio.sleep(0.01)
    try:
        await asyncio.sleep(0.2)
        assert not serving.done()
        async with DaemonClient(autostart=False) as c:
            assert await c.call("ping")
    finally:
        daemon.stop()
        await asyncio.wait_for(serving, timeout=BUDGET)
        await daemon.aclose()
