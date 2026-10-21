"""Historical launch-policy facts preserve raw values and explicit field presence."""

from __future__ import annotations

import json

import pytest

from theater.daemon.runtime.launch_policy import decode_launch_policy
from theater.harness.contracts.runtime import RuntimeHost


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
