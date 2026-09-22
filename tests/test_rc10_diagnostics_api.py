"""Focused public adapters for existing usage, statistics, and bus facts."""

from __future__ import annotations

import asyncio
import json

import pytest

from theater import paths, protocol
from theater.daemon.frontend import router as router_mod
from theater.daemon.frontend.diagnostic_handlers import DIAGNOSTIC_HANDLERS
from theater.daemon.frontend.handlers import PUBLIC_HANDLERS
from theater.frontend.capabilities import METHOD_CATALOG, PUBLIC_API_MAJOR, PUBLIC_API_MINOR
from theater.frontend.schemas import validator_for


def _request(request_id: int, method: str, params: dict | None = None) -> bytes:
    return protocol.encode({"id": request_id, "method": method, "params": params or {}})


def _handshake() -> bytes:
    return _request(
        1,
        "frontend.handshake",
        {
            "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
            "client_id": "diagnostic-api-test",
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
def diagnostics_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_mod, "PUBLIC_HANDLERS", {**PUBLIC_HANDLERS, **DIAGNOSTIC_HANDLERS})


def _validate(method: str, response: dict) -> None:
    assert response["ok"] is True
    validator_for(METHOD_CATALOG[method].result_schema_id).validate(response["result"])


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

    responses = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.usage.totals", {"since": 99.0}),
            _request(3, "frontend.usage.summary", {"since": 99.0}),
            _request(4, "frontend.usage.by_harness", {"since": 99.0}),
            _request(5, "frontend.usage.by_harness", {"detailed": True}),
            _request(6, "frontend.usage.summary", {"since": None}),
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

    responses = await _exchange(
        [
            _handshake(),
            _request(2, "frontend.stats.get"),
            _request(3, "frontend.bus.tail", {"after_id": first_id, "limit": 1}),
            _request(4, "frontend.bus.tail", {"limit": 501}),
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
