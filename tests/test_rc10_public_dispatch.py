"""RC10 public routing, isolation, discovery, and initial safe reads."""

from __future__ import annotations

import json

from tests.support.frontend_wire import exchange, handshake, request
from theater import protocol
from theater.daemon.frontend import validation as validation_mod
from theater.daemon.frontend.validation import error_response, success_response
from theater.daemon.operations import OperationService
from theater.daemon.plugins.credentials import credential_verifier
from theater.models import ProviderRecord, now


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
