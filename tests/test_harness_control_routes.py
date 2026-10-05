"""Compatibility-record wiring and control-route reporting.

One record per harness feeds the dashboard line: wiring plus the five
control routes, derived from the same rules the daemon's resolver applies.
"""

from __future__ import annotations

import logging

import pytest

from theater import harness as harness_registry
from theater.harness.contracts.runtime import RuntimeCompatibility

_LEGACY_ROUTES = {
    "send": "tmux",
    "steer": "tmux",
    "queue_followup": "tmux",
    "settings_update": "unavailable",
    "interrupt": "tmux",
}
# Only harnesses declaring that Enter steers a running turn (codex, pi) steer over a pane.
_LEGACY_ROUTES_NO_STEER = {**_LEGACY_ROUTES, "steer": "unavailable"}
_PI_ROUTES = {
    "send": "native",
    "steer": "unavailable",
    "queue_followup": "native",
    "settings_update": "native",
    "interrupt": "native",
}
_OPENCODE_ROUTES = {
    "send": "native",
    "steer": "unavailable",
    "queue_followup": "native",
    "settings_update": "unavailable",
    "interrupt": "tmux",
}
_SUPPORTED = RuntimeCompatibility(
    supported=True,
    policy="qualified-test",
    native_version="1.0.0",
)
_OUT_OF_RANGE = RuntimeCompatibility(
    supported=False,
    policy="qualified-test",
    native_version="0.158.0",
    reason="wiring=auto selects legacy below the qualified range",
)


@pytest.mark.parametrize(
    ("harness_name", "result", "expected"),
    [
        pytest.param(
            "pi",
            _SUPPORTED,
            {"status": "native-compatible", "wiring": "native", "routes": _PI_ROUTES},
            id="pi-native-steer-unavailable",
        ),
        pytest.param(
            "opencode",
            _SUPPORTED,
            {"status": "native-compatible", "wiring": "native", "routes": _OPENCODE_ROUTES},
            id="opencode-native-interrupt-tmux",
        ),
        pytest.param(
            "codex",
            _OUT_OF_RANGE,
            {"status": "outside-qualified-range", "wiring": "legacy", "routes": _LEGACY_ROUTES},
            id="codex-outside-range-legacy-routes",
        ),
        pytest.param(
            "claude",
            _OUT_OF_RANGE,
            {
                "status": "legacy-only",
                "wiring": "legacy",
                "routes": _LEGACY_ROUTES_NO_STEER,
                "qualified_range": None,
                "reason": "no native runtime; controls use the provider terminal",
            },
            id="claude-runtimeless-range-hidden",
        ),
        pytest.param(
            "vibe",
            None,
            {"status": "legacy-only", "wiring": "legacy", "routes": _LEGACY_ROUTES_NO_STEER},
            id="vibe-no-probe-legacy-routes",
        ),
    ],
)
def test_native_compatibility_record_reports_wiring_and_routes(harness_name, result, expected):
    record = harness_registry.native_compatibility_record(
        harness_registry.HARNESSES[harness_name], installed=True, result=result
    )
    assert expected.items() <= record.items()
    assert set(record["routes"]) == {
        "send",
        "steer",
        "queue_followup",
        "settings_update",
        "interrupt",
    }


def test_a_probe_without_a_version_is_unknown_not_outside_range():
    undetermined = RuntimeCompatibility(
        supported=False,
        policy="qualified-test",
        native_version=None,
        reason="version command failed",
    )
    record = harness_registry.native_compatibility_record(
        harness_registry.HARNESSES["codex"], installed=True, result=undetermined
    )
    assert record["status"] == "unknown"
    assert record["reason"] == "version command failed"
    assert record["qualified_range"] == ">=0.148.0,<=0.159.1"


def test_a_probe_exception_reason_is_bounded():
    error = RuntimeError("boom " * 100)
    record = harness_registry.native_compatibility_record(
        harness_registry.HARNESSES["codex"], installed=True, error=error
    )
    assert record["status"] == "unknown"
    assert record["reason"] == f"compatibility probe failed: RuntimeError: {error}"[:200]
    assert len(record["reason"]) <= 200


def test_an_uninstalled_harness_reports_no_wiring_or_routes():
    record = harness_registry.native_compatibility_record(
        harness_registry.HARNESSES["codex"], installed=False, result=_SUPPORTED
    )
    assert record["status"] == "not-installed"
    assert record["wiring"] is None
    assert record["routes"] is None


async def test_harnesses_surfaces_a_probe_exception_as_unknown(client, daemon, monkeypatch, caplog):
    monkeypatch.setattr(harness_registry.shutil, "which", lambda binary: f"/bin/{binary}")

    async def probe(name, callback, context, *, configuration):
        del callback, context, configuration
        if name == "codex":
            raise RuntimeError("version command crashed")
        return _SUPPORTED

    monkeypatch.setattr(daemon.compatibility_probes, "probe", probe)
    with caplog.at_level(logging.WARNING, logger="theater.daemon.rpc.spawning"):
        rows = {row["name"]: row for row in await client.call("harnesses")}

    codex = rows["codex"]["native_compatibility"]
    assert codex["status"] == "unknown"
    assert codex["reason"] == "compatibility probe failed: RuntimeError: version command crashed"
    assert codex["wiring"] == "legacy"
    assert codex["routes"]["interrupt"] == "tmux"
    assert any("codex" in message for message in caplog.messages)


def test_only_harnesses_that_declare_it_steer_over_a_terminal():
    declared = {
        name
        for name, harness in harness_registry.HARNESSES.items()
        if harness.controls is not None and harness.controls.steer is not None
    }
    # Enter steers a running turn in the Codex and Pi TUIs; the rest are unverified.
    assert declared == {"codex", "pi"}
