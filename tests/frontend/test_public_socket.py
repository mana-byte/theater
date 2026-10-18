"""Public socket: negotiation, peer identity, connection roles, validation and frame limits."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import struct

import pytest

from tests.support.frontend_wire import exchange, handshake, request
from theater import protocol
from theater.daemon.frontend import validation as validation_mod
from theater.daemon.frontend.validation import error_response, success_response
from theater.daemon.operations import OperationService
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.runtime.socket import (
    PeerIdentityError,
    handle_connection,
    peer_uid_from_socket,
    verify_peer_uid,
)
from theater.frontend.capabilities import CAPABILITIES, PUBLIC_API_MAJOR, PUBLIC_API_MINOR
from theater.models import ProviderRecord, now


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


def _handshake(
    *,
    role: str = "operator",
    channel: str = "rpc",
    provider_id: str | None = None,
    credential: str | None = None,
) -> bytes:
    identity = {} if provider_id is None else {"provider_id": provider_id}
    if provider_id is not None:
        identity["provider_credential"] = credential
    return handshake("dispatch-test", role=role, channel=channel, **identity)


async def test_connection_classification_is_permanent_both_directions(daemon):
    private_then_public = await exchange([protocol.request(1, "ping"), _handshake()])
    public_then_private = await exchange([_handshake(), protocol.request(2, "ping")])
    unknown_then_public = await exchange([protocol.request(1, "not-a-method"), _handshake()])

    assert private_then_public[0]["result"]["pong"] is True
    assert private_then_public[1]["error"]["code"] == "wrong_connection_role"
    assert public_then_private[0]["ok"] is True
    assert public_then_private[1]["id"] == 2
    assert public_then_private[1]["error"]["code"] == "wrong_connection_role"
    assert unknown_then_public[0]["error"]["code"] == "unknown_method"
    assert unknown_then_public[1]["ok"] is True


async def test_public_validation_is_strict_and_curated(daemon):
    unknown_field = request(2, "frontend.health.get", {}, surprise=True)
    private_forwarding_name = request(3, "frontend.shutdown", {})
    malformed = b'{"id":4,"method":"frontend.health.get","params":{oops}\n'
    responses = await exchange([_handshake(), unknown_field, private_forwarding_name, malformed])

    assert responses[1]["id"] == 2
    assert responses[1]["error"]["code"] == "bad_request"
    assert responses[2]["id"] == 3
    assert responses[2]["error"]["code"] == "unknown_method"
    assert responses[3]["id"] == 0
    assert responses[3]["error"]["code"] == "bad_request"


async def test_contract_schema_health_and_real_participant_read(daemon):
    participant = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/project")
    responses = await exchange(
        [
            _handshake(),
            request(2, "frontend.contract.get"),
            request(3, "frontend.schemas.get"),
            request(4, "frontend.health.get"),
            request(5, "frontend.participants.get", {"participant_id": participant.id}),
        ]
    )

    contract = responses[1]["result"]
    assert "frontend.handshake" in contract["methods"]
    assert "frontend.participants.get" in contract["methods"]
    assert "terminal.deliver" in contract["callbacks"]
    assert any(uri.endswith("envelopes.json") for uri in responses[2]["result"]["resources"])
    assert responses[3]["result"]["status"] == "ok"
    projected = responses[4]["result"]
    assert projected["participant_id"] == participant.id
    assert projected["cwd"] == "/tmp/project"
    assert "tmux_pane" not in projected and "session_id" not in projected


async def test_operation_handlers_use_the_daemon_composition(daemon):
    assert isinstance(daemon.operation_service, OperationService)
    responses = await exchange(
        [
            _handshake(),
            request(2, "frontend.operations.get", {"operation_id": "missing-operation"}),
        ]
    )

    assert responses[1]["id"] == 2
    assert responses[1]["error"]["code"] == "not_found"
    assert responses[1]["error"]["details"] == {"operation_id": "missing-operation"}


async def test_provider_role_and_callback_channel_are_isolated(daemon):
    credential = "provider-secret-with-enough-entropy"
    timestamp = now()
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(
            ProviderRecord(
                provider_id="provider-a",
                selector="tmux",
                kind="tmux",
                credential_verifier=credential_verifier(credential),
                configuration_version=1,
                capabilities=("terminal-provider.v1",),
                limits={},
                generation=7,
                last_report_revision=None,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )

    provider_rpc = await exchange(
        [
            _handshake(role="provider", provider_id="provider-a", credential=credential),
            request(2, "frontend.participants.get", {"participant_id": "missing"}),
        ]
    )
    callback = await exchange(
        [
            _handshake(
                role="provider",
                channel="callback",
                provider_id="provider-a",
                credential=credential,
            ),
            request(2, "frontend.contract.get"),
        ]
    )

    assert provider_rpc[0]["result"]["provider_generation"] == 7
    assert provider_rpc[1]["error"]["code"] == "wrong_connection_role"
    assert callback[1]["error"]["code"] == "wrong_connection_role"


async def test_existing_private_client_still_uses_private_dispatch(client):
    assert (await client.call("ping"))["pong"] is True


async def test_complete_frame_limit_counts_the_newline(daemon, monkeypatch):
    monkeypatch.setattr(protocol, "MAX_MESSAGE_BYTES", 48)
    response = (await exchange([b" " * 48 + b"\n"]))[0]
    assert response["id"] == 0
    assert response["error"]["code"] == "too_large"


def test_oversized_success_becomes_correlated_bounded_error(monkeypatch):
    monkeypatch.setattr(validation_mod, "MAX_FRAME_BYTES", 256)

    response = success_response("frontend.contract.get", 19, {"payload": "x" * 400})

    decoded = json.loads(response)
    assert len(response) <= 256
    assert decoded["id"] == 19
    assert decoded["ok"] is False
    assert decoded["error"]["code"] == "too_large"


def test_oversized_error_details_are_dropped_without_recursion(monkeypatch):
    monkeypatch.setattr(validation_mod, "MAX_FRAME_BYTES", 256)

    response = error_response(
        23,
        "bad_request",
        "request was rejected",
        details={"payload": "x" * 400},
    )

    decoded = json.loads(response)
    assert len(response) <= 256
    assert decoded["id"] == 23
    assert decoded["error"]["code"] == "bad_request"
    assert "details" not in decoded["error"]
