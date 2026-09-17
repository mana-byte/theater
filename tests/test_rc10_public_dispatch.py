"""RC10 public routing, isolation, discovery, and initial safe reads."""

from __future__ import annotations

import asyncio
import json

from theater import paths, protocol
from theater.daemon.plugins.credentials import credential_verifier
from theater.frontend.capabilities import PUBLIC_API_MAJOR, PUBLIC_API_MINOR
from theater.models import ProviderRecord, now


def _request(request_id: int, method: str, params: dict | None = None, **extra) -> bytes:
    return protocol.encode({"id": request_id, "method": method, "params": params or {}, **extra})


def _handshake(
    *,
    role: str = "operator",
    channel: str = "rpc",
    provider_id: str | None = None,
    credential: str | None = None,
) -> bytes:
    params = {
        "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
        "client_id": "dispatch-test",
        "role": role,
        "channel": channel,
        "required_capabilities": [],
    }
    if provider_id is not None:
        params.update(provider_id=provider_id, provider_credential=credential)
    return _request(1, "frontend.handshake", params)


async def _exchange(frames: list[bytes]) -> list[dict]:
    reader, writer = await asyncio.open_unix_connection(
        str(paths.socket_path()), limit=max(protocol.MAX_MESSAGE_BYTES, 1024)
    )
    try:
        responses = []
        for frame in frames:
            writer.write(frame)
            await writer.drain()
            responses.append(json.loads(await protocol.read_message(reader)))
        return responses
    finally:
        writer.close()
        await writer.wait_closed()


async def test_connection_classification_is_permanent_both_directions(daemon):
    private_then_public = await _exchange([protocol.request(1, "ping"), _handshake()])
    public_then_private = await _exchange([_handshake(), protocol.request(2, "ping")])
    unknown_then_public = await _exchange([protocol.request(1, "not-a-method"), _handshake()])

    assert private_then_public[0]["result"]["pong"] is True
    assert private_then_public[1]["error"]["code"] == "wrong_connection_role"
    assert public_then_private[0]["ok"] is True
    assert public_then_private[1]["id"] == 2
    assert public_then_private[1]["error"]["code"] == "wrong_connection_role"
    assert unknown_then_public[0]["error"]["code"] == "unknown_method"
    assert unknown_then_public[1]["ok"] is True


async def test_public_validation_is_strict_and_curated(daemon):
    unknown_field = _request(2, "frontend.health.get", {}, surprise=True)
    private_forwarding_name = _request(3, "frontend.shutdown", {})
    malformed = b'{"id":4,"method":"frontend.health.get","params":{oops}\n'
    responses = await _exchange([_handshake(), unknown_field, private_forwarding_name, malformed])

    assert responses[1]["id"] == 2
    assert responses[1]["error"]["code"] == "bad_request"
    assert responses[2]["id"] == 3
    assert responses[2]["error"]["code"] == "unknown_method"
    assert responses[3]["id"] == 0
    assert responses[3]["error"]["code"] == "bad_request"


async def test_contract_schema_health_and_real_participant_read(daemon):
    participant = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/project")
    responses = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.contract.get"),
            _request(3, "frontend.schemas.get"),
            _request(4, "frontend.health.get"),
            _request(5, "frontend.participants.get", {"participant_id": participant.id}),
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

    provider_rpc = await _exchange(
        [
            _handshake(role="provider", provider_id="provider-a", credential=credential),
            _request(2, "frontend.participants.get", {"participant_id": "missing"}),
        ]
    )
    callback = await _exchange(
        [
            _handshake(
                role="provider",
                channel="callback",
                provider_id="provider-a",
                credential=credential,
            ),
            _request(2, "frontend.contract.get"),
        ]
    )

    assert provider_rpc[0]["result"]["provider_generation"] == 7
    assert provider_rpc[1]["error"]["code"] == "wrong_connection_role"
    assert callback[1]["error"]["code"] == "wrong_connection_role"


async def test_existing_private_client_still_uses_private_dispatch(client):
    assert (await client.call("ping"))["pong"] is True


async def test_complete_frame_limit_counts_the_newline(daemon, monkeypatch):
    monkeypatch.setattr(protocol, "MAX_MESSAGE_BYTES", 48)
    response = (await _exchange([b" " * 48 + b"\n"]))[0]
    assert response["id"] == 0
    assert response["error"]["code"] == "too_large"
