"""Historical launch-policy facts preserve raw values and explicit field presence."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.support.native_lifecycle import RoutingIO
from theater.daemon.controls.routing import _pinned_route
from theater.daemon.rpc import controls as rpc_controls
from theater.daemon.runtime import recovery
from theater.daemon.runtime.launch_policy import decode_launch_policy
from theater.daemon.spawning import frontend
from theater.harness.contracts.runtime import (
    CapabilityUnavailableReason,
    ControlTransport,
    RuntimeCapability,
    RuntimeHost,
)


@pytest.mark.parametrize("raw", [None, "", "invalid", "null", "[]", "42", '"text"'])
def test_non_object_launch_policy_has_no_pinned_fields(raw):
    facts = decode_launch_policy(raw)
    assert facts.fields == {}
    assert not facts.runtime_host_present
    assert facts.runtime_host is None
    assert not facts.control_routes_present
    assert facts.control_routes is None


@pytest.mark.parametrize(
    ("host", "decoded"),
    [
        (None, None),
        (False, None),
        (7, None),
        ([], None),
        ("unknown", None),
        ("frontend", RuntimeHost.FRONTEND),
        ("detached_backend", RuntimeHost.DETACHED_BACKEND),
    ],
)
def test_present_runtime_host_is_distinct_from_valid_host(host, decoded):
    facts = decode_launch_policy(json.dumps({"runtime_host": host}))
    assert facts.runtime_host_present
    assert facts.fields["runtime_host"] == host
    assert facts.runtime_host is decoded


@pytest.mark.parametrize("routes", [None, [], {"send": []}, {"send": {"future": True}}])
def test_raw_selection_and_route_values_are_retained(routes):
    fields = {
        "approval": False,
        "model": 7,
        "reasoning_effort": ["future"],
        "control_routes": routes,
        "future": {"nested": [1, None]},
    }
    facts = decode_launch_policy(json.dumps(fields))
    assert facts.fields == fields
    assert facts.control_routes_present
    assert facts.control_routes == (routes if isinstance(routes, dict) else None)


@pytest.mark.parametrize(
    ("raw", "frontend_expected", "rpc_expected"),
    [
        (None, True, RuntimeHost.FRONTEND),
        ("invalid", True, RuntimeHost.FRONTEND),
        ("[]", True, RuntimeHost.FRONTEND),
        ('{"runtime_host":null}', False, RuntimeHost.FRONTEND),
        ('{"runtime_host":"unknown"}', False, RuntimeHost.FRONTEND),
        ('{"runtime_host":"detached_backend"}', False, RuntimeHost.DETACHED_BACKEND),
        ('{"runtime_host":"frontend"}', True, RuntimeHost.FRONTEND),
    ],
)
def test_frontend_pinning_and_rpc_host_fallback_stay_distinct(
    monkeypatch, raw, frontend_expected, rpc_expected
):
    harness = SimpleNamespace(runtime=SimpleNamespace(host=RuntimeHost.FRONTEND))
    binding = SimpleNamespace(harness="fixture", launch_policy=raw)
    monkeypatch.setattr(frontend, "get_harness", lambda _: harness)
    monkeypatch.setattr(rpc_controls, "HARNESSES", {"fixture": harness})
    daemon = SimpleNamespace(store=SimpleNamespace(get_runtime_binding=lambda _: binding))
    target = SimpleNamespace(id="p1", harness="fixture")

    assert frontend.is_frontend_binding(binding) is frontend_expected
    assert rpc_controls._runtime_host(daemon, target) is rpc_expected


@pytest.mark.parametrize(
    ("raw", "transport", "reason"),
    [
        (None, None, None),
        ("invalid", None, None),
        ('{"control_routes":null}', None, CapabilityUnavailableReason.THEATER_POLICY),
        ('{"control_routes":[]}', None, CapabilityUnavailableReason.THEATER_POLICY),
        (
            '{"control_routes":{"send":"native_runtime"}}',
            ControlTransport.NATIVE_RUNTIME,
            None,
        ),
        (
            '{"control_routes":{"send":"legacy_tmux"}}',
            ControlTransport.PROVIDER_TERMINAL,
            None,
        ),
    ],
)
def test_pinned_route_keeps_manifest_fallback_and_malformed_refusal(raw, transport, reason):
    route = _pinned_route(raw, RuntimeCapability.SEND)
    if transport is None and reason is None:
        assert route is None
    else:
        assert route is not None
        assert route.transport is transport
        assert route.unavailable_reason is reason


@pytest.mark.parametrize("transport", [[], {}])
def test_unhashable_pinned_transport_keeps_type_error(transport):
    raw = json.dumps({"control_routes": {"send": transport}})
    with pytest.raises(TypeError):
        _pinned_route(raw, RuntimeCapability.SEND)


def _recovery_create(monkeypatch, raw):
    manifest = SimpleNamespace(factory=lambda context: context)
    monkeypatch.setattr(recovery, "get_harness", lambda _: SimpleNamespace(binary="fixture"))
    monkeypatch.setattr("theater.daemon.runtime.wiring.runtime_manifest_of", lambda _: manifest)
    monkeypatch.setattr(recovery, "_runtime_token_file", lambda *_: None)
    daemon = SimpleNamespace(runtime_io=RoutingIO())
    binding = SimpleNamespace(
        harness="fixture",
        launch_policy=raw,
        participant_id="p1",
        backend_generation=1,
        endpoint=None,
        native_session_id=None,
    )
    factory = recovery._runtime_factory(daemon, binding, SimpleNamespace(cwd="/tmp"))
    assert factory is not None
    return factory[1]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, None, None)),
        ("invalid", (None, None, None)),
        (
            '{"approval":"manual","model":"fixture-model","reasoning_effort":"high"}',
            ("manual", "fixture-model", "high"),
        ),
    ],
)
async def test_native_recovery_passes_raw_selection_to_runtime_context(monkeypatch, raw, expected):
    context = await _recovery_create(monkeypatch, raw)()
    assert (context.approval, context.model, context.reasoning_effort) == expected


@pytest.mark.parametrize("raw", ['{"approval":false}', '{"model":7}', '{"reasoning_effort":[]}'])
async def test_native_recovery_does_not_filter_malformed_selection(monkeypatch, raw):
    with pytest.raises(ValueError, match="runtime context"):
        await _recovery_create(monkeypatch, raw)()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, None, None)),
        ("invalid", (None, None, None)),
        ('{"approval":" ","model":"","reasoning_effort":7}', (" ", None, None)),
        (
            '{"approval":"manual","model":"fixture-model","reasoning_effort":"high"}',
            ("manual", "fixture-model", "high"),
        ),
    ],
)
async def test_passive_frontend_keeps_truthy_string_selection(monkeypatch, raw, expected):
    runtime = SimpleNamespace(
        host=RuntimeHost.FRONTEND, channel=SimpleNamespace(channel=SimpleNamespace(id="live"))
    )
    monkeypatch.setattr(frontend, "get_harness", lambda _: SimpleNamespace(runtime=runtime))
    captured = {}

    async def start_listener(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(frontend, "start_frontend_listener", start_listener)
    credential = SimpleNamespace(harness="fixture", token="token")
    daemon = SimpleNamespace(
        store=SimpleNamespace(get_channel_credential=lambda *_: credential),
        frontend_runtime_host=None,
        runtime_manager=None,
        runtime_io=None,
        observer=SimpleNamespace(live=None),
    )
    binding = SimpleNamespace(
        harness="fixture",
        launch_policy=raw,
        endpoint="unix:///tmp/frontend.sock",
        backend_generation=1,
    )
    assert await frontend.restore_frontend_listener(daemon, binding, SimpleNamespace(id="p1"))
    assert (captured["approval"], captured["model"], captured["reasoning_effort"]) == expected
