"""Public catalog, usage, statistics and bus read adapters, exercised through the socket."""

from __future__ import annotations

from functools import partial

import pytest

from tests.support.frontend_wire import exchange, handshake, request
from theater.daemon.frontend import router as router_mod
from theater.daemon.frontend.catalog_handlers import CATALOG_HANDLERS
from theater.daemon.frontend.diagnostic_handlers import DIAGNOSTIC_HANDLERS
from theater.daemon.frontend.handlers import PUBLIC_HANDLERS
from theater.daemon.plugins.credentials import credential_verifier
from theater.frontend.capabilities import (
    METHOD_CATALOG,
    TERMINAL_PROVIDER_CAPABILITY,
)
from theater.frontend.dto.catalogs import NativeCompatibility
from theater.frontend.schemas import validator_for
from theater.models import ProviderRecord, now

_handshake = partial(handshake, "read-adapter-test")


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
                "binaries": ["fixture-wrapper"],
                "installed": True,
                "error": None,
                "approvals": ["manual", "edits"],
                "native_compatibility": {"status": "native-compatible"},
            }
        ]

    monkeypatch.setattr("theater.daemon.frontend.catalog_handlers._harnesses", harnesses)
    missing = (await exchange([_handshake(), request(2, "frontend.catalogs.harnesses")]))[1]

    _validate("frontend.catalogs.harnesses", missing)
    entry = missing["result"]["items"][0]
    assert entry["installed"] is True
    assert entry["supported_wiring"] == ["legacy"]
    assert entry["requires_terminal"] is True
    assert entry["provider_ready"] is False
    assert entry["launch_available"] is False
    assert entry["approvals"] == ["manual", "edits"]
    assert entry["binaries"] == ["fixture-wrapper"]
    assert entry["reason"] == "provider_unavailable"

    with daemon.store.write_unit() as unit:
        daemon.store.providers.register(_provider(capabilities=()), connection=unit.connection)
    incompatible = (
        await exchange(
            [_handshake(), request(2, "frontend.catalogs.harnesses", {"provider": "selected"})]
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
        await exchange(
            [_handshake(), request(2, "frontend.catalogs.harnesses", {"provider": "selected"})]
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
    responses = await exchange(
        [
            _handshake(),
            request(2, "frontend.catalogs.models"),
            request(3, "frontend.skills.list"),
        ]
    )

    _validate("frontend.catalogs.models", responses[1])
    _validate("frontend.skills.list", responses[2])
    assert responses[1]["result"]["items"]
    assert {"harness", "models", "supported"} <= set(responses[1]["result"]["items"][0])
    assert "rejections" in responses[2]["result"]
    skill_name = responses[2]["result"]["items"][0]["name"]

    loaded = (
        await exchange([_handshake(), request(2, "frontend.skills.load", {"name": skill_name})])
    )[1]
    _validate("frontend.skills.load", loaded)
    assert loaded["result"]["name"] == skill_name
    assert isinstance(loaded["result"]["content"], str)


def test_native_compatibility_dto_round_trips_wiring_and_routes():
    record = {
        "status": "outside-qualified-range",
        "installed_version": "2.0.1",
        "qualified_range": "==2.0.0",
        "policy": "codex-qualified",
        "reason": "wiring=auto selects legacy below the qualified range",
        "wiring": "legacy",
        "routes": {
            "send": "tmux",
            "steer": "tmux",
            "queue_followup": "tmux",
            "settings_update": "unavailable",
            "interrupt": "tmux",
        },
        "future_key": 1,
    }
    dto = NativeCompatibility.from_wire(record)
    assert dto.wiring == "legacy"
    assert dict(dto.routes) == record["routes"]
    assert dto.extra == {"future_key": 1}
    wire = dto.to_wire()
    assert wire["wiring"] == "legacy"
    assert wire["routes"] == record["routes"]
    assert wire["future_key"] == 1

    malformed = NativeCompatibility.from_wire({"status": "unknown", "wiring": 3, "routes": "nope"})
    assert malformed.wiring is None
    assert dict(malformed.routes) == {}
    assert dict(NativeCompatibility.from_wire({"status": "legacy-only"}).routes) == {}


@pytest.fixture
def diagnostics_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_mod, "PUBLIC_HANDLERS", {**PUBLIC_HANDLERS, **DIAGNOSTIC_HANDLERS})


async def test_usage_adapters_keep_absolute_since_and_existing_summary_facts(
    daemon, diagnostics_dispatch, monkeypatch: pytest.MonkeyPatch
):
    del diagnostics_dispatch
    monkeypatch.setattr("theater.daemon.frontend.diagnostic_handlers.now", lambda: 100.0)
    monkeypatch.setattr("theater.daemon.rpc.usage.now", lambda: 100.0)
    assert daemon.store.record_usage(
        participant_id="participant-a",
        tree_root_id="participant-a",
        usage_key="usage-a",
        ts=100.0,
        model="fixture-model",
        harness="fixture",
        input_tokens=3,
        output_tokens=5,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        reasoning_output_tokens=0,
        cost_microcents=7,
    )

    responses = await exchange(
        [
            _handshake(),
            request(2, "frontend.usage.totals", {"since": 99.0}),
            request(3, "frontend.usage.summary", {"since": 99.0}),
            request(4, "frontend.usage.by_harness", {"since": 99.0}),
            request(5, "frontend.usage.by_harness", {"detailed": True}),
            request(6, "frontend.usage.summary", {"since": None}),
        ]
    )

    for method, response in zip(
        ("frontend.usage.totals", "frontend.usage.summary", "frontend.usage.by_harness"),
        responses[1:4],
        strict=True,
    ):
        _validate(method, response)
    assert responses[1]["result"]["since"] == 99.0
    assert responses[1]["result"]["input_tokens"] == 3
    assert responses[2]["result"]["windowed"]["cost_microcents"] == 7
    assert responses[3]["result"]["since"] == {
        "day": 99.0,
        "week": 99.0,
        "month": 99.0,
    }
    _validate("frontend.usage.by_harness", responses[4])
    detailed = responses[4]["result"]
    fixture = next(row for row in detailed["harnesses"] if row["harness"] == "fixture")
    assert fixture["models"][0]["model"] == "fixture-model"
    assert detailed["totals"]["today"]["input_tokens"] == 3

    _validate("frontend.usage.summary", responses[5])
    assert responses[5]["result"]["since"] is None
    assert responses[5]["result"]["windowed"] == responses[5]["result"]["all_time"]


async def test_stats_and_bus_tail_are_bounded_public_pages(daemon, diagnostics_dispatch):
    del diagnostics_dispatch
    first_id = daemon.store.bus_append("diagnostic.first", payload={"ordinal": 1})
    second_id = daemon.store.bus_append("diagnostic.second", payload={"ordinal": 2})

    responses = await exchange(
        [
            _handshake(),
            request(2, "frontend.stats.get"),
            request(3, "frontend.bus.tail", {"after_id": first_id, "limit": 1}),
            request(4, "frontend.bus.tail", {"limit": 501}),
        ]
    )

    _validate("frontend.stats.get", responses[1])
    _validate("frontend.bus.tail", responses[2])
    assert {"coverage", "harnesses", "refusals", "since"} <= set(responses[1]["result"])
    row = responses[2]["result"]["items"][-1]
    assert {key: row[key] for key in ("id", "from_id", "to_id", "kind", "payload")} == {
        "id": second_id,
        "from_id": None,
        "to_id": None,
        "kind": "diagnostic.second",
        "payload": {"ordinal": 2},
    }
    assert isinstance(row["ts"], float)
    assert responses[2]["result"]["next_after_id"] == second_id
    assert responses[2]["result"]["next_cursor"] == str(second_id)
    assert responses[3]["ok"] is False
    assert responses[3]["error"]["code"] == "bad_request"


async def test_usage_by_participant_filters_ids_and_since(daemon, diagnostics_dispatch):
    del diagnostics_dispatch
    for participant_id, usage_key, timestamp, input_tokens in (
        ("participant-a", "a-old", 10.0, 1),
        ("participant-a", "a-new", 20.0, 2),
        ("participant-b", "b-new", 30.0, 4),
    ):
        assert daemon.store.record_usage(
            participant_id=participant_id,
            tree_root_id="participant-a",
            usage_key=usage_key,
            ts=timestamp,
            model="fixture-model",
            harness="fixture",
            input_tokens=input_tokens,
            output_tokens=input_tokens * 2,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
            reasoning_output_tokens=0,
            cost_microcents=input_tokens * 3,
        )

    responses = await exchange(
        [
            _handshake(),
            request(
                2,
                "frontend.usage.by_participant",
                {"since": 15.0, "participant_ids": ["participant-a"]},
            ),
            request(3, "frontend.usage.by_participant", {"limit": 1}),
        ]
    )

    _validate("frontend.usage.by_participant", responses[1])
    assert responses[1]["result"] == {
        "since": 15.0,
        "truncated": False,
        "participants": [
            {
                "participant_id": "participant-a",
                "harness": "fixture",
                "models": ["fixture-model"],
                "input_tokens": 2,
                "output_tokens": 4,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "reasoning_output_tokens": 0,
                "cost_microcents": 6,
                "first_at": 20.0,
                "last_at": 20.0,
            }
        ],
    }
    _validate("frontend.usage.by_participant", responses[2])
    assert responses[2]["result"]["truncated"] is True
    assert responses[2]["result"]["participants"][0]["participant_id"] == "participant-b"
