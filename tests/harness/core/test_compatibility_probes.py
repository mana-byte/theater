"""Native compatibility records, probe caching and probe outcome reporting."""

from __future__ import annotations

import asyncio
import logging
import subprocess

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from theater import harness as harness_registry
from theater import proc, timing
from theater.daemon.harness_runtime.compatibility import CompatibilityProbeCache
from theater.daemon.worktrees.repository import _git
from theater.harness.builtin.plugins.codex.runtime_plan import qualified_versions_range_text
from theater.harness.contracts.runtime import RuntimeCompatibility, RuntimeProbeContext
from theater.observability import tracing
from theater.observability.catalog import PROC_PS_COMM

# One compatibility record per harness feeds the dashboard line: wiring plus five control routes,
# derived from the same rules the daemon's resolver applies.

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
    native_version="1.0.0",
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
    assert record["qualified_range"] == qualified_versions_range_text()


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


@pytest.mark.parametrize("change", ["binary", "configuration", "callback", "expiry"])
async def test_catalog_probes_coalesce_and_invalidate(tmp_path, change):
    binary = tmp_path / "agent"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    clock = [0.0]
    cache = CompatibilityProbeCache(clock=lambda: clock[0])
    configuration = object()
    context = RuntimeProbeContext(binary=str(binary))
    calls = []

    def callback(context):
        calls.append(context)
        return RuntimeCompatibility(supported=True, native_version=str(len(calls)))

    async def probe():
        return await cache.probe("agent", callback, context, configuration=configuration)

    results = await asyncio.gather(*(probe() for _ in range(4)))
    assert len(calls) == 1
    assert all(result.native_version == "1" for result in results)

    if change == "binary":
        binary.write_text("#!/bin/sh\nexit 42\n")
    elif change == "configuration":
        configuration = object()
    elif change == "callback":
        original = callback

        def callback(context):
            return original(context)
    else:
        clock[0] = 61
    assert (await probe()).native_version == "2"
    assert (await probe()).native_version == "2"
    assert len(calls) == 2


async def test_changed_binary_during_probe_does_not_publish_stale_compatibility(tmp_path):
    binary = tmp_path / "agent"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    cache = CompatibilityProbeCache()

    def callback(context):
        binary.write_text("#!/bin/sh\nexit 42\n")
        return RuntimeCompatibility(supported=True)

    assert (
        await cache.probe(
            "agent", callback, RuntimeProbeContext(binary=str(binary)), configuration=None
        )
        is None
    )


@pytest.fixture
def spans(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_get_tracer", lambda: provider.get_tracer("probe-test"))
    try:
        yield exporter
    finally:
        provider.shutdown()


@pytest.mark.parametrize(
    "error,is_error",
    [
        (subprocess.CalledProcessError(1, "ps", output="", stderr=""), False),
        (subprocess.CalledProcessError(1, "ps", output="", stderr="permission denied"), True),
        (subprocess.TimeoutExpired("ps", 5), True),
    ],
)
def test_process_probe_only_treats_empty_absence_as_normal(monkeypatch, spans, error, is_error):
    def check_output(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(subprocess, "check_output", check_output)
    assert proc.comm(123) == ""
    (span,) = spans.get_finished_spans()
    assert (span.status.status_code is StatusCode.ERROR) is is_error


@pytest.mark.parametrize("returncode,expected", [(1, (0,)), (1, (0, 1)), (128, (0, 1))])
def test_git_probe_expected_codes_are_explicit(monkeypatch, spans, returncode, expected):
    monkeypatch.setattr(
        subprocess, "run", lambda args, **_kwargs: subprocess.CompletedProcess(args, returncode)
    )
    assert _git(["git", "show-ref"], expected_returncodes=expected).returncode == returncode
    (span,) = spans.get_finished_spans()
    assert (span.status.status_code is StatusCode.ERROR) is (returncode not in expected)


def test_cancelled_span_preserves_cancellation_without_error_status(spans):
    with pytest.raises(asyncio.CancelledError), timing.span(PROC_PS_COMM, pid=123):
        raise asyncio.CancelledError
    (span,) = spans.get_finished_spans()
    assert span.attributes["result"] == "cancelled"
    assert span.status.status_code is StatusCode.UNSET
