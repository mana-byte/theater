"""The daemon socket client: reply alignment, message-size limits and protocol contracts."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import get_type_hints

import pytest

from theater import client as client_mod
from theater import paths, protocol
from theater.client import DaemonClient
from theater.daemon.lock import DaemonLock
from theater.daemon.methods import METHODS
from theater.daemon.persistence.transactions import WriteUnit as DaemonWriteUnit
from theater.daemon.server import Daemon
from theater.mcp.client_pool import DaemonClientPool
from theater.protocol import (
    BUS_TAIL_MAX_WAIT_SECONDS,
    BUS_TAIL_METHOD,
    BusEvent,
    BusTailParams,
    MessageTooLarge,
    ProviderReportFacts,
    ProviderReportResult,
    RemoteError,
    WriteUnit,
)


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


async def test_mcp_pool_isolates_slow_calls_bounds_connections_and_never_replays(daemon_factory):
    started = {name: asyncio.Event() for name in ("slow", "second")}
    release = asyncio.Event()

    async def handler(msg):
        name = msg["params"]["name"]
        if name in started:
            started[name].set()
            await release.wait()
        return [protocol.ok(msg["id"], name)]

    daemon = await daemon_factory(handler)
    pool = DaemonClientPool(size=2, autostart=False)
    slow = asyncio.create_task(pool.call("ping", name="slow"))
    await asyncio.wait_for(started["slow"].wait(), 5)
    assert await asyncio.wait_for(pool.call("ping", name="fast"), 10) == "fast"
    second = asyncio.create_task(pool.call("ping", name="second"))
    await asyncio.wait_for(started["second"].wait(), 5)
    queued = asyncio.create_task(pool.call("ping", name="queued"))
    await asyncio.sleep(0)
    assert not queued.done()
    assert daemon.connections == 2
    queued.cancel()
    slow.cancel()
    await asyncio.gather(queued, slow, return_exceptions=True)
    assert await asyncio.wait_for(pool.call("ping", name="recovered"), 10) == "recovered"
    closing = asyncio.create_task(pool.aclose())
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="closed"):
        await pool.call("ping", name="after-close")
    assert not closing.done()
    release.set()
    assert await second == "second"
    await asyncio.wait_for(closing, 10)
    assert [msg["params"]["name"] for msg in daemon.requests] == [
        "slow",
        "fast",
        "second",
        "recovered",
    ]
    assert all(client._writer is None for client in pool._clients)


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


async def test_non_object_reply_drops_connection_and_reconnects(daemon_factory):
    async def handler(msg):
        if msg["id"] == 1:
            return [b"5\n"]
        return [protocol.ok(msg["id"], "ok")]

    daemon = await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(ConnectionError):
            await client.call("ping")
        assert client._writer is None
        assert await client.call("ping") == "ok"
        assert daemon.connections == 2
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


async def test_client_span_carries_meta_through_remote_error(daemon_factory, monkeypatch):
    observed = {}

    class Span:
        def __enter__(self):
            return fields

        def __exit__(self, exc_type, exc, traceback):
            observed["exception"] = exc
            return False

    def fake_span(spec, **fields):
        observed["spec"] = spec
        observed["fields"] = fields
        return Span()

    class Fields(dict):
        def measure(self, _name):
            from contextlib import nullcontext

            return nullcontext()

    fields = Fields()

    carrier = {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}
    monkeypatch.setattr(client_mod, "timing_span", fake_span)
    monkeypatch.setattr("theater.observability.tracing.inject_trace_context", lambda: carrier)

    async def handler(msg):
        return [protocol.err(msg["id"], "not_found", "missing")]

    daemon = await daemon_factory(handler)
    client = _client()
    try:
        with pytest.raises(RemoteError, match="not_found"):
            await client.call("participants.get", participant_id="missing")
    finally:
        await client.aclose()

    assert daemon.requests[0]["_meta"] == carrier
    assert observed["spec"].key == "RPC_CLIENT"
    assert observed["fields"] == {"method": "participants.get", "call_id": None, "slow_ms": None}
    assert fields["request_id"] == 1
    assert isinstance(observed["exception"], RemoteError)


async def test_cancelled_lock_wait_is_timed_without_poisoning_active_call(daemon_factory, caplog):
    caplog.set_level(logging.DEBUG, logger="theater.timing")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(msg):
        entered.set()
        await release.wait()
        return [protocol.ok(msg["id"], "ok")]

    daemon = await daemon_factory(handler)
    client = _client()
    active = asyncio.create_task(client.call("first"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        queued = asyncio.create_task(client.call("queued"))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        (record,) = [r for r in caplog.records if r.__dict__.get("theater.result") == "cancelled"]
        assert record.__dict__["theater.lock_wait_ms"] >= 0
        assert "theater.connect_ms" not in record.__dict__
        assert "theater.roundtrip_ms" not in record.__dict__
        release.set()
        assert await active == "ok"
        assert await client.call("next") == "ok"
        assert daemon.connections == 1
        assert [request["id"] for request in daemon.requests] == [1, 2]
    finally:
        release.set()
        await active
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


def test_await_gets_its_own_budget():
    """jobs.await blocks on purpose and must not inherit the default."""
    default = DaemonClient._timeout_for("ping", {})
    waiting = DaemonClient._timeout_for("jobs.await", {"max_wait": 60.0})
    assert waiting > default + 59


def test_await_timeout_handles_huge_max_wait():
    assert DaemonClient._timeout_for("jobs.await", {"max_wait": 10**400}) == client_mod.CALL_TIMEOUT


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


#: Comfortably past asyncio's 64 KiB default, small enough to stay quick.
BIG = 300_000


#: A limit small enough to overrun on purpose, for the tests that need to see
#: what happens *at* the ceiling rather than below it.
TINY = 4096


@pytest.fixture
def echo(monkeypatch):
    """Register a method whose request and reply sizes the test dictates."""

    async def _echo(daemon, params: dict) -> dict:
        return {
            "size": len(params.get("payload", "")),
            "reply": "y" * int(params.get("reply_size", 0)),
        }

    monkeypatch.setitem(METHODS, "echo", _echo)
    return _echo


async def test_a_big_reply_does_not_disturb_the_next_call(client, echo):
    """The point of the fix is the *next* call, not the big one.

    Before it, the oversized reply left the connection out of step and every
    later call on it read someone else's bytes.
    """
    assert len((await client.call("echo", reply_size=BIG))["reply"]) == BIG
    assert (await client.call("ping"))["pong"] is True
    assert (await client.call("echo", payload="z" * BIG))["size"] == BIG


async def test_an_overrun_reply_drops_the_connection(daemon, echo, monkeypatch):
    """An overrun desynchronises the stream, so the connection cannot be kept.

    Reconnecting is lazy, so the proof is in two parts: the writer is gone
    immediately, and the following call works rather than reading the tail of
    the reply that overran.
    """
    monkeypatch.setattr(protocol, "MAX_MESSAGE_BYTES", TINY)
    c = DaemonClient(autostart=False)
    try:
        with pytest.raises(MessageTooLarge):
            await c.call("echo", reply_size=TINY * 8)
        assert c._writer is None

        monkeypatch.setattr(protocol, "MAX_MESSAGE_BYTES", BIG)
        assert (await c.call("ping"))["pong"] is True
    finally:
        await c.aclose()


async def test_the_daemon_answers_an_oversized_request_and_keeps_serving(
    theater_home, echo, monkeypatch
):
    """One absurd prompt must not cost an agent the rest of its session.

    The daemon is built inside the test rather than taken from the fixture
    because a server's limit is fixed when it binds, and this test needs a
    small one.
    """
    monkeypatch.setattr(protocol, "MAX_MESSAGE_BYTES", TINY)
    d = Daemon(harnesses={})
    await d.start()
    c = DaemonClient(autostart=False)
    try:
        # Client limit back up, so only the daemon's ceiling is under test.
        monkeypatch.setattr(protocol, "MAX_MESSAGE_BYTES", BIG)
        with pytest.raises(RemoteError) as caught:
            await c.call("echo", payload="x" * (TINY * 8))
        assert caught.value.code == "too_large"

        # Same connection: the daemon must have drained the tail of the
        # request it refused, or this reads a fragment of it as JSON.
        assert c._writer is not None
        assert (await c.call("ping"))["pong"] is True
    finally:
        await c.aclose()
        await d.aclose()


async def test_read_message_reports_the_overrun():
    reader = asyncio.StreamReader(limit=16)
    reader.feed_data(b"x" * 200 + b"\n")
    reader.feed_eof()
    with pytest.raises(MessageTooLarge):
        await protocol.read_message(reader)


async def test_drain_message_resynchronises_the_stream():
    """After draining, the reader is positioned on the next message.

    Both messages are in the buffer before the first read, which is the case
    `readline` could not survive: it clears the whole buffer on an overrun,
    taking the innocent message with it.
    """
    reader = asyncio.StreamReader(limit=16)
    reader.feed_data(b"x" * 200 + b"\n" + b'{"id":1}\n')
    reader.feed_eof()

    with pytest.raises(MessageTooLarge):
        await protocol.read_message(reader)
    await protocol.drain_message(reader)

    assert await protocol.read_message(reader) == b'{"id":1}\n'


async def test_drain_message_stops_at_end_of_stream():
    """A line that never ends must not become an infinite loop."""
    reader = asyncio.StreamReader(limit=16)
    reader.feed_data(b"x" * 200)
    reader.feed_eof()

    with pytest.raises(MessageTooLarge):
        await protocol.read_message(reader)
    await asyncio.wait_for(protocol.drain_message(reader), timeout=5)

    assert await protocol.read_message(reader) == b""


def test_the_bus_tail_and_provider_report_contracts_are_exported() -> None:
    assert BUS_TAIL_METHOD == "frontend.bus.tail"
    assert BUS_TAIL_MAX_WAIT_SECONDS == 30.0
    assert {"wait_seconds", "kinds"} <= set(BusTailParams.__optional_keys__)
    assert "invalidated_terminals" in ProviderReportFacts.__optional_keys__
    assert "presence_invalidated" not in ProviderReportResult.__annotations__
    assert get_type_hints(BusEvent)["ts"] is float


def test_write_unit_mirrors_daemon_interface() -> None:
    def public(cls: type) -> set[str]:
        return {
            name
            for name in vars(cls)
            if name in {"__enter__", "__exit__"} or not name.startswith("_")
        }

    assert public(WriteUnit) == public(DaemonWriteUnit)
