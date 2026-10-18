"""RC10 public negotiation and kernel-owned local-peer identity."""

from __future__ import annotations

import asyncio
import os
import socket
import struct

import pytest

from tests.support.frontend_wire import exchange, handshake
from theater import protocol
from theater.daemon.runtime.socket import (
    PeerIdentityError,
    handle_connection,
    peer_uid_from_socket,
    verify_peer_uid,
)
from theater.frontend.capabilities import CAPABILITIES, PUBLIC_API_MAJOR, PUBLIC_API_MINOR


def test_linux_and_bsd_peer_uid_extraction_seams():
    class LinuxSocket:
        def getsockopt(self, level, option, size):
            assert size == struct.calcsize("3i")
            return struct.pack("3i", 123, 456, 789)

    class BsdSocket:
        def fileno(self):
            return 42

    assert peer_uid_from_socket(LinuxSocket(), platform="linux") == 456
    assert (
        peer_uid_from_socket(
            BsdSocket(), platform="darwin", getpeereid=lambda descriptor: (descriptor + 1, 7)
        )
        == 43
    )


def test_missing_and_wrong_peer_uid_fail_closed():
    class MissingWriter:
        def get_extra_info(self, _name):
            return None

    with pytest.raises(PeerIdentityError, match="peer socket"):
        verify_peer_uid(MissingWriter())
    with pytest.raises(PeerIdentityError, match="peer socket"):
        verify_peer_uid(object())

    left, right = __import__("socket").socketpair()
    try:

        class Writer:
            def get_extra_info(self, _name):
                return left

        with pytest.raises(PeerIdentityError, match="does not match"):
            verify_peer_uid(Writer(), expected_uid=os.geteuid() + 1)
    finally:
        left.close()
        right.close()


async def test_missing_peer_identity_closes_before_private_dispatch():
    class Writer:
        def __init__(self):
            self.responses = []
            self.closed = False

        def write(self, value):
            self.responses.append(value)

        async def drain(self):
            pass

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    class Daemon:
        def __init__(self):
            self._conns = set()
            self.dispatched = False

        async def _dispatch(self, _line):
            self.dispatched = True
            return protocol.ok(1, True)

    reader = asyncio.StreamReader()
    reader.feed_data(protocol.request(1, "ping"))
    reader.feed_eof()
    writer = Writer()
    daemon = Daemon()

    await handle_connection(daemon, reader, writer, private_methods={"ping": object()})

    assert daemon.dispatched is False
    assert writer.responses == []
    assert writer.closed is True


async def test_unclassified_connection_expires_without_read_or_dispatch():
    left, right = socket.socketpair()

    class Writer:
        def __init__(self):
            self.closed = False

        def get_extra_info(self, name):
            return left if name == "socket" else None

        def write(self, _value):
            raise AssertionError("an idle unclassified peer must not receive a response")

        def close(self):
            self.closed = True

        async def wait_closed(self):
            pass

    class Daemon:
        def __init__(self):
            self._conns = set()
            self.dispatched = False

        async def _dispatch(self, _line):
            self.dispatched = True
            return protocol.ok(1, True)

    writer = Writer()
    daemon = Daemon()
    try:
        await handle_connection(
            daemon,
            asyncio.StreamReader(),
            writer,
            private_methods={"ping": object()},
            handshake_timeout=0,
        )
    finally:
        left.close()
        right.close()

    assert daemon.dispatched is False
    assert writer.closed is True


async def test_handshake_negotiates_minor_and_returns_durable_contract(daemon):
    first = (await exchange([handshake(minor=PUBLIC_API_MINOR + 9, required=[CAPABILITIES[0]])]))[0]
    second = (await exchange([handshake()]))[0]

    assert first["ok"] is True
    assert first["result"]["api"] == {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR}
    assert first["result"]["daemon_instance_id"] == second["result"]["daemon_instance_id"]
    assert first["result"]["limits"]["max_in_flight"] == 1
    assert set(first["result"]["capabilities"]) == set(CAPABILITIES)


@pytest.mark.parametrize(
    ("frame", "code"),
    [
        (protocol.request(1, "frontend.health.get"), "handshake_required"),
        (handshake(major=PUBLIC_API_MAJOR + 1), "incompatible_api"),
        (handshake(required=["future.required.v1"]), "missing_capability"),
    ],
)
async def test_handshake_order_and_compatibility_refusals(daemon, frame, code):
    response = (await exchange([frame]))[0]
    assert response["id"] == 1
    assert response["error"]["code"] == code
