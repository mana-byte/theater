"""Focused public catalog adapters, exercised through the frontend socket."""

from __future__ import annotations

import asyncio
import json

import pytest

from theater import paths, protocol
from theater.daemon.frontend import router as router_mod
from theater.daemon.frontend.catalog_handlers import CATALOG_HANDLERS
from theater.daemon.frontend.handlers import PUBLIC_HANDLERS
from theater.daemon.plugins.credentials import credential_verifier
from theater.frontend.capabilities import (
    METHOD_CATALOG,
    PUBLIC_API_MAJOR,
    PUBLIC_API_MINOR,
    TERMINAL_PROVIDER_CAPABILITY,
)
from theater.frontend.schemas import validator_for
from theater.models import ProviderRecord, now


def _request(request_id: int, method: str, params: dict | None = None) -> bytes:
    return protocol.encode({"id": request_id, "method": method, "params": params or {}})


def _handshake() -> bytes:
    return _request(
        1,
        "frontend.handshake",
        {
            "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
            "client_id": "catalog-api-test",
            "role": "operator",
            "channel": "rpc",
            "required_capabilities": [],
        },
    )


async def _exchange(frames: list[bytes]) -> list[dict]:
    reader, writer = await asyncio.open_unix_connection(str(paths.socket_path()))
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


@pytest.fixture
def catalog_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_mod, "PUBLIC_HANDLERS", {**PUBLIC_HANDLERS, **CATALOG_HANDLERS})


def _validate(method: str, response: dict) -> None:
    assert response["ok"] is True
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(response["result"])


def _provider(*, capabilities: tuple[str, ...]) -> ProviderRecord:
    timestamp = now()
    return ProviderRecord(
        provider_id="provider-a",
        selector="selected",
        kind="fixture",
        credential_verifier=credential_verifier("catalog-provider-credential"),
        configuration_version=1,
        capabilities=capabilities,
        limits={},
        generation=1,
        last_report_revision=None,
        created_at=timestamp,
        updated_at=timestamp,
    )


async def test_harness_catalog_separates_installation_provider_readiness_and_native_claims(
    daemon, monkeypatch: pytest.MonkeyPatch, catalog_dispatch
):
    del catalog_dispatch

    async def harnesses(_daemon, _params):
        return [
            {
                "name": "fixture",
                "binary": "fixture-cli",
                "installed": True,
                "error": None,
                "approvals": ["manual", "edits"],
                "native_compatibility": {"status": "native-compatible"},
            }
        ]

    monkeypatch.setattr("theater.daemon.frontend.catalog_handlers._harnesses", harnesses)
    missing = (await _exchange([_handshake(), _request(2, "frontend.catalogs.harnesses")]))[1]

    _validate("frontend.catalogs.harnesses", missing)
    entry = missing["result"]["items"][0]
    assert entry["installed"] is True
    assert entry["supported_wiring"] == ["legacy"]
    assert entry["requires_terminal"] is True
    assert entry["provider_ready"] is False
    assert entry["launch_available"] is False
    assert entry["approvals"] == ["manual", "edits"]
    assert entry["reason"] == "provider_unavailable"

    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(_provider(capabilities=()), connection=unit.connection)
    incompatible = (
        await _exchange(
            [_handshake(), _request(2, "frontend.catalogs.harnesses", {"provider": "selected"})]
        )
    )[1]

    _validate("frontend.catalogs.harnesses", incompatible)
    assert incompatible["result"]["items"][0]["reason"] == "provider_incompatible"
    assert incompatible["result"]["items"][0]["provider_ready"] is False


async def test_harness_catalog_uses_a_live_compatible_selected_provider(
    daemon, monkeypatch: pytest.MonkeyPatch, catalog_dispatch
):
    del catalog_dispatch

    async def harnesses(_daemon, _params):
        return [{"name": "fixture", "binary": "fixture-cli", "installed": True, "error": None}]

    monkeypatch.setattr("theater.daemon.frontend.catalog_handlers._harnesses", harnesses)
    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(
            _provider(capabilities=(TERMINAL_PROVIDER_CAPABILITY,)), connection=unit.connection
        )
    monkeypatch.setattr(
        daemon.terminal_service.connections, "health", lambda _provider_id: "online"
    )

    response = (
        await _exchange(
            [_handshake(), _request(2, "frontend.catalogs.harnesses", {"provider": "selected"})]
        )
    )[1]

    _validate("frontend.catalogs.harnesses", response)
    entry = response["result"]["items"][0]
    assert entry["compatible"] is True
    assert entry["provider_ready"] is True
    assert entry["launch_available"] is True
    assert entry["provider"]["provider_id"] == "provider-a"


async def test_model_and_skill_catalogs_reuse_daemon_owned_discovery(daemon, catalog_dispatch):
    del catalog_dispatch
    responses = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.catalogs.models"),
            _request(3, "frontend.skills.list"),
        ]
    )

    _validate("frontend.catalogs.models", responses[1])
    _validate("frontend.skills.list", responses[2])
    assert responses[1]["result"]["items"]
    assert {"harness", "models", "supported"} <= set(responses[1]["result"]["items"][0])
    assert "rejections" in responses[2]["result"]
    skill_name = responses[2]["result"]["items"][0]["name"]

    loaded = (
        await _exchange([_handshake(), _request(2, "frontend.skills.load", {"name": skill_name})])
    )[1]
    _validate("frontend.skills.load", loaded)
    assert loaded["result"]["name"] == skill_name
    assert isinstance(loaded["result"]["content"], str)
