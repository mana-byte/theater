"""Big messages on the daemon socket.

asyncio's stream reader defaults to a 64 KiB line limit, and neither end of
the socket used to override it. That is about ten assistant turns, so every
part of Theater that returns a lot of text at once -- `read_transcript`,
which is unclipped by design, `bus.tail` at any real depth, a prompt with a
pasted file in it -- crossed the ceiling as a matter of course.

The failure was not a clean one. `readline` raises a bare `ValueError`, which
no caller was catching, *and* it leaves the connection desynchronised: the
bytes it had buffered are dropped, the rest of the oversized line is still on
the wire. On the client that meant one big reply broke every later call on
that connection, which is what agents were reporting as Theater "sometimes
working and sometimes erroring".

These tests run against a real daemon on a real unix socket. A mocked reader
cannot reproduce the part that matters -- what is left in the stream after an
overrun -- and that is the whole bug.
"""

from __future__ import annotations

import asyncio

import pytest

from theater import protocol
from theater.client import DaemonClient
from theater.daemon.methods import METHODS
from theater.daemon.server import Daemon
from theater.protocol import MessageTooLarge, RemoteError

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


# ---- the ceiling is gone -----------------------------------------------


async def test_a_big_reply_does_not_disturb_the_next_call(client, echo):
    """The point of the fix is the *next* call, not the big one.

    Before it, the oversized reply left the connection out of step and every
    later call on it read someone else's bytes.
    """
    assert len((await client.call("echo", reply_size=BIG))["reply"]) == BIG
    assert (await client.call("ping"))["pong"] is True
    assert (await client.call("echo", payload="z" * BIG))["size"] == BIG


# ---- and when something does exceed it ---------------------------------


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


# ---- the two readers, on their own --------------------------------------


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
    await asyncio.wait_for(protocol.drain_message(reader), timeout=2)

    assert await protocol.read_message(reader) == b""
