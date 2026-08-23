"""Reply/request alignment on the daemon socket.

The bug these guard against was silent and long-lived: one call slower than the
read timeout left its reply in the socket buffer, and every later call on that
connection read the *previous* call's answer. An MCP server holds one client
for the life of an agent session, so a single slow call made every subsequent
tool call return someone else's result -- which looked, from the agent's side,
like Theater "sometimes working and sometimes erroring".

These tests run a fake daemon on a real unix socket rather than patching the
stream: the failure lives in the interaction between a cancelled `readline`
and the bytes still queued in the kernel, and a mocked reader cannot have that
interaction.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from theater import client as client_mod
from theater import paths, protocol
from theater.client import DaemonClient
from theater.daemon.lock import DaemonLock
from theater.protocol import RemoteError
from theater.tmux import client as tmux


class FakeDaemon:
    """A daemon that answers however the test tells it to.

    Requests on one connection are served strictly in order, as the real
    server does (it reads, answers, then reads again), because that ordering
    is what leaves a late reply queued for the next caller to pick up.
    """

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[dict] = []
        self.connections = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._serve, str(paths.socket_path()))

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _serve(self, reader, writer) -> None:
        self.connections += 1
        try:
            while True:
                line = await reader.readline()
                if not line:
                    return
                msg = json.loads(line)
                self.requests.append(msg)
                chunks = await self.handler(msg)
                if chunks is None:  # the handler asked us to hang up
                    return
                for chunk in chunks:
                    writer.write(chunk)
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()


@pytest.fixture
async def daemon_factory():
    """Start a fake daemon and tear it down with the test."""
    started: list[FakeDaemon] = []

    async def make(handler) -> FakeDaemon:
        d = FakeDaemon(handler)
        await d.start()
        started.append(d)
        return d

    yield make
    for d in started:
        await d.stop()


@pytest.fixture
def fast_timeout(monkeypatch):
    """Shrink the read budget so a 'slow' reply is slow in milliseconds."""
    monkeypatch.setattr(client_mod, "CALL_TIMEOUT", 0.2)


def _client() -> DaemonClient:
    # autostart off: a test that cannot reach its fake daemon should fail
    # loudly, not fork a real one.
    return DaemonClient(autostart=False)


# ---- the regression ----------------------------------------------------


async def test_slow_reply_does_not_leak_into_the_next_call(daemon_factory, fast_timeout):
    async def handler(msg):
        if msg["params"].get("slow"):
            await asyncio.sleep(0.5)
        return [protocol.ok(msg["id"], f"answer-to-{msg['id']}")]

    await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(TimeoutError):
            await client.call("ping", slow=True)

        # The reply to call 1 is still in flight. Before the fix this returned
        # "answer-to-1"; every call after it was off by one, forever.
        assert await client.call("ping") == "answer-to-2"
        assert await client.call("ping") == "answer-to-3"
    finally:
        await client.aclose()


async def test_timeout_reconnects_instead_of_reusing_a_poisoned_socket(
    daemon_factory, fast_timeout
):
    async def handler(msg):
        if msg["params"].get("slow"):
            await asyncio.sleep(0.5)
        return [protocol.ok(msg["id"], "ok")]

    daemon = await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(TimeoutError):
            await client.call("ping", slow=True)
        assert client._writer is None, "a timed-out connection must be dropped"

        await client.call("ping")
        assert daemon.connections == 2, "the next call must open a fresh socket"
    finally:
        await client.aclose()


async def test_failed_call_is_not_retried(daemon_factory, fast_timeout):
    """A resend would double-type a prompt into a live pane.

    Recovery is limited to the connection: the call that failed stays failed,
    because `send` and `spawn` are not idempotent.
    """

    async def handler(msg):
        if msg["method"] == "send":
            await asyncio.sleep(0.5)
        return [protocol.ok(msg["id"], "ok")]

    daemon = await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(TimeoutError):
            await client.call("send", prompt="hello")
        await client.call("ping")
    finally:
        await client.aclose()

    sends = [r for r in daemon.requests if r["method"] == "send"]
    assert len(sends) == 1


# ---- reply matching ----------------------------------------------------


async def test_reply_to_an_older_request_is_skipped(daemon_factory):
    """Defence in depth for a read abandoned without dropping the socket.

    Ids only grow and the lock allows one call in flight, so a reply numbered
    below the request we are waiting on can only be the leftover of a call
    that gave up.
    """

    async def handler(msg):
        if msg["id"] == 1:
            return [protocol.ok(1, "first")]
        return [
            protocol.ok(msg["id"] - 1, "leftover"),
            protocol.ok(msg["id"], "mine"),
        ]

    await daemon_factory(handler)
    client = _client()
    try:
        assert await client.call("ping") == "first"
        assert await client.call("ping") == "mine"
    finally:
        await client.aclose()


async def test_reply_to_a_request_we_never_sent_is_fatal(daemon_factory):
    """An id ahead of ours means the stream is unreadable, not merely stale."""

    async def handler(msg):
        return [protocol.ok(msg["id"] + 1, "from the future")]

    await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(ConnectionError):
            await client.call("ping")
        assert client._writer is None
    finally:
        await client.aclose()


async def test_unparseable_request_error_is_surfaced(daemon_factory):
    """The daemon answers id 0 when it cannot echo one; that is still our reply."""

    async def handler(msg):
        return [protocol.err(0, "bad_request", "malformed json")]

    await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(RemoteError) as exc:
            await client.call("ping")
        assert exc.value.code == "bad_request"
    finally:
        await client.aclose()


async def test_remote_error_is_raised_not_desynced(daemon_factory):
    """An error reply consumes the request, so the next call stays aligned."""

    async def handler(msg):
        if msg["method"] == "boom":
            return [protocol.err(msg["id"], "not_found", "no such thing")]
        return [protocol.ok(msg["id"], f"answer-to-{msg['id']}")]

    daemon = await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(RemoteError):
            await client.call("boom")
        assert await client.call("ping") == "answer-to-2"
        assert daemon.connections == 1, "an error reply must not drop the socket"
    finally:
        await client.aclose()


async def test_daemon_hangup_reconnects_on_the_next_call(daemon_factory):
    async def handler(msg):
        if msg["params"].get("hangup"):
            return None  # close without answering
        return [protocol.ok(msg["id"], "ok")]

    daemon = await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(ConnectionError):
            await client.call("ping", hangup=True)
        # Before the fix, `connect` returned early on a stale writer and every
        # later call raised "daemon closed the connection" forever.
        assert await client.call("ping") == "ok"
        assert daemon.connections == 2
    finally:
        await client.aclose()


# ---- timeout budget ----------------------------------------------------


def test_read_timeout_outlasts_the_tmux_ceiling():
    """The daemon shells out to tmux; giving up first is what caused the desync.

    `send` runs up to three tmux invocations (presence, literal keys, Enter),
    so the client's budget has to clear that with room to spare.
    """
    assert client_mod.CALL_TIMEOUT >= 3 * tmux.RUN_TIMEOUT


def test_await_gets_its_own_budget():
    """jobs.await blocks on purpose and must not inherit the default."""
    default = DaemonClient._timeout_for("ping", {})
    waiting = DaemonClient._timeout_for("jobs.await", {"max_wait": 60.0})
    assert waiting > default + 59


# ---- autostart herd ----------------------------------------------------


async def test_autostart_skips_the_spawn_when_a_daemon_holds_the_lock(theater_home, monkeypatch):
    """Eight clients failing to connect must not fork eight daemons.

    Only one can win the lock, so the other seven would read the config,
    install plugins, open the database and exit — enough work, on a cold
    start, to push the winner past the connect timeout that made them all
    spawn in the first place.
    """
    forked: list[list[str]] = []
    monkeypatch.setattr(client_mod.subprocess, "Popen", lambda cmd, **kw: forked.append(cmd))

    held = DaemonLock()
    held.acquire()
    try:
        await DaemonClient()._start_daemon()
        assert forked == []
    finally:
        held.release()


async def test_autostart_spawns_when_nothing_holds_the_lock(theater_home, monkeypatch):
    """The suppression must not become a refusal to ever start one."""
    forked: list[list[str]] = []
    monkeypatch.setattr(client_mod.subprocess, "Popen", lambda cmd, **kw: forked.append(cmd))

    await DaemonClient()._start_daemon()
    assert len(forked) == 1
    assert forked[0][:2] == [sys.executable, "-m"]
    assert "daemon" in forked[0]
    assert "--stderr-token" in forked[0]
