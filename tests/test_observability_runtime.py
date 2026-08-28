"""Runtime: configure, shutdown, validation, views, gauge cache, rollback."""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest


def _run(code):
    r = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "OK" in r.stdout


VALIDATION_TESTS = [
    ('configure(role="bogus")', "role"),
    ('configure(role="daemon", otlp_protocol="udp")', "otlp_protocol"),
    ('configure(role="daemon", service_name="  ")', "service_name"),
    ('configure(role="daemon", log_level="VERBOSE")', "log_level"),
    ('configure(role="daemon", otlp_endpoint="localhost:4317")', "http or https"),
    ('configure(role="daemon", otlp_endpoint="http://host?q=1")', "query/fragment"),
    ('configure(role="daemon", otlp_endpoint="http://[")', "invalid otlp_endpoint"),
]


@pytest.mark.parametrize("call,match", VALIDATION_TESTS)
def test_validation(call, match):
    _run(f"""
        from theater.observability.runtime import ObservabilityError, configure
        try:
            {call}
        except ObservabilityError as exc:
            assert {match!r} in str(exc)
        else:
            raise AssertionError("validation accepted bad input")
        print("OK")
    """)


@pytest.mark.parametrize(
    "protocol,endpoint,expected",
    [
        ("grpc", None, ("http://localhost:4317",) * 3),
        (
            "http",
            "https://collector.example/prefix/",
            (
                "https://collector.example/prefix/v1/traces",
                "https://collector.example/prefix/v1/metrics",
                "https://collector.example/prefix/v1/logs",
            ),
        ),
    ],
)
def test_endpoint_resolution(protocol, endpoint, expected):
    from theater.observability.runtime import _resolve_endpoints

    assert _resolve_endpoints(protocol, endpoint) == expected


def test_disabled_no_sdk():
    _run("""
        import sys
        from theater.observability.runtime import configure, is_configured
        h = configure(role="daemon", otlp_enabled=False)
        assert not any(name.startswith("opentelemetry.sdk") for name in sys.modules)
        assert h.signal_bridge is None
        assert not h.closed
        h.shutdown()
        assert h.closed and is_configured()
        print("OK")
    """)


def test_regie_local_log_works_without_otel(tmp_path):
    _run(f"""
        import logging
        from pathlib import Path
        from theater.observability.runtime import configure
        path = Path({str(tmp_path / "logs" / "regie" / "pane-7.log")!r})
        path.parent.mkdir(parents=True)
        h = configure(role="regie", otlp_enabled=False, log_path=path)
        logging.getLogger("theater.regie").warning("regie-visible")
        h.shutdown()
        assert "regie-visible" in path.read_text()
        print("OK")
    """)


def test_shutdown_idempotent():
    _run("""
        from theater.observability.runtime import RuntimeHandle
        h = RuntimeHandle()
        h.shutdown()
        h.shutdown()
        assert h.closed
        print("OK")
    """)


def test_second_configuration_is_rejected():
    _run("""
        from theater.observability.runtime import ObservabilityError, configure
        h = configure(role="daemon")
        h.shutdown()
        try:
            configure(role="daemon")
        except ObservabilityError:
            pass
        else:
            raise AssertionError("second configure succeeded")
        print("OK")
    """)


def test_non_daemon_null_handler():
    _run("""
        import logging
        from theater.observability.runtime import configure
        h = configure(role="mcp", otlp_enabled=True)
        otel = logging.getLogger("opentelemetry")
        assert any(isinstance(x, logging.NullHandler) for x in otel.handlers)
        h.shutdown()
        print("OK")
    """)


def test_enabled_runtime_owns_then_removes_signal_bridge():
    _run("""
        from theater.observability.runtime import configure
        h = configure(role="mcp", otlp_enabled=True)
        bridge = h.signal_bridge
        assert bridge is not None and bridge.active
        h.shutdown()
        assert h.signal_bridge is None
        assert not bridge.active
        print("OK")
    """)


def test_shutdown_flushes_queued_agent_logs_and_spans():
    _run("""
        from unittest.mock import patch
        from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter
        from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from theater.observability import runtime

        class Metrics(MetricExporter):
            def export(self, metrics_data, timeout_millis=10_000, **kwargs):
                return MetricExportResult.SUCCESS
            def force_flush(self, timeout_millis=10_000):
                return True
            def shutdown(self, timeout_millis=30_000, **kwargs):
                pass

        spans = InMemorySpanExporter()
        logs = InMemoryLogRecordExporter()
        metrics = Metrics()

        def exporters(protocol, endpoints, staged):
            return spans, metrics, logs

        with patch.object(runtime, "_build_exporters", exporters):
            h = runtime.configure(
                role="daemon", otlp_enabled=True, export_interval_ms=60_000
            )
            bridge = h.signal_bridge
            assert bridge is not None
            assert bridge.emit_span(
                "agent.request", attributes={"agent.harness": "codex"},
                start_time_ns=100, end_time_ns=200,
            ) is not None
            assert bridge.emit_log(
                "agent.record", body="accepted", attributes={"agent.revision": 1}
            )
            assert spans.get_finished_spans() == ()
            assert logs.get_finished_logs() == ()
            h.shutdown()

        assert [span.name for span in spans.get_finished_spans()] == ["agent.request"]
        span = spans.get_finished_spans()[0]
        assert span.resource.attributes["service.name"] == "theater"
        assert span.resource.attributes["theater.process.role"] == "daemon"
        exported = logs.get_finished_logs()
        assert len(exported) == 1
        assert exported[0].log_record.event_name == "agent.record"
        assert exported[0].log_record.attributes["agent.revision"] == 1
        assert exported[0].resource.attributes["service.name"] == "theater"
        assert exported[0].resource.attributes["theater.process.role"] == "daemon"
        print("OK")
    """)


def test_otel_rollback_does_not_store_signal_bridge():
    _run("""
        from unittest.mock import patch
        from theater.observability import runtime
        handle = runtime.RuntimeHandle()
        with patch.object(runtime, "RuntimeHandle", return_value=handle), patch.object(
            runtime, "_attach_otel_logging", side_effect=RuntimeError("boom")
        ):
            try:
                runtime.configure(role="daemon", otlp_enabled=True)
            except RuntimeError:
                pass
            else:
                raise AssertionError("configure succeeded")
        assert handle.signal_bridge is None
        print("OK")
    """)


def test_views_built_from_catalog(monkeypatch):
    from opentelemetry.sdk.metrics import view as view_mod
    from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation

    from theater.observability.runtime import _build_views

    definitions = []
    monkeypatch.setattr(view_mod, "View", lambda **kwargs: definitions.append(kwargs) or kwargs)
    views = _build_views()

    assert views == definitions
    assert definitions
    assert all(
        isinstance(definition["aggregation"], ExponentialBucketHistogramAggregation)
        for definition in definitions
    )


def test_views_include_external_histograms_but_not_counters(monkeypatch):
    from opentelemetry.sdk.metrics import view as view_mod

    from theater.observability.metrics import MetricKind, MetricSpec
    from theater.observability.runtime import _build_views

    definitions = []
    monkeypatch.setattr(view_mod, "View", lambda **kwargs: definitions.append(kwargs) or kwargs)
    _build_views(
        (
            MetricSpec(
                "theater.external.duration", "External duration", "ms", MetricKind.HISTOGRAM
            ),
            MetricSpec("theater.external.total", "External count", "1", MetricKind.COUNTER),
        )
    )
    names = {definition["instrument_name"] for definition in definitions}
    assert "theater.external.duration" in names
    assert "theater.external.total" not in names


def test_views_passed_to_meter_provider_constructor():
    """MeterProvider receives views= in constructor."""
    _run("""
        import opentelemetry.sdk.metrics as mm
        captured = {}
        orig = mm.MeterProvider.__init__
        def spy(self, **kw):
            captured['views'] = kw.get('views')
            return orig(self, **kw)
        mm.MeterProvider.__init__ = spy
        try:
            from theater.observability.runtime import configure
            h = configure(role="daemon", otlp_enabled=True)
            h.shutdown()
        finally:
            mm.MeterProvider.__init__ = orig
        assert captured.get('views'), f"no views: {captured}"
        print("OK")
    """)


def test_same_gauge_cache_sampler_and_callbacks():
    """Sampler writes to same cache callbacks read; callback returns Observation(5)."""
    _run("""
        from theater.observability.runtime import configure
        from theater.observability.metrics import create_active_gauge_sampler
        h = configure(role="daemon", otlp_enabled=True)
        try:
            sampler = create_active_gauge_sampler(1.0, {"theater.participants.live": lambda: 5})
            assert sampler is not None
            from theater.observability.engine import metric_bridge
            assert metric_bridge().gauge_cache is sampler.cache
            # Write via sampler's cache, read via registered callback
            sampler.cache.set("theater.participants.live", 5)
            cbs = sampler.cache.make_callbacks()
            result = cbs["theater.participants.live"](None)
            assert len(result) == 1 and result[0].value == 5
        finally:
            h.shutdown()
        print("OK")
    """)


def test_existing_provider_is_rejected():
    _run("""
        from unittest.mock import patch
        from theater.observability.runtime import ObservabilityError, configure
        from opentelemetry.trace import set_tracer_provider
        from opentelemetry.sdk.trace import TracerProvider

        real = TracerProvider()
        set_tracer_provider(real)
        try:
            try:
                configure(role="daemon", otlp_enabled=True)
                assert False, "should have raised"
            except ObservabilityError:
                pass
        finally:
            with __import__('contextlib').suppress(Exception):
                real.shutdown()
        print("OK")
    """)


def test_failed_local_setup_does_not_poison_configure():
    _run("""
        from pathlib import Path
        from unittest.mock import patch
        from theater.observability.runtime import configure, is_configured
        with patch("theater.observability.logging.make_rotating_handler", side_effect=OSError("x")):
            try:
                configure(role="daemon", log_path=Path("/unused"))
            except OSError:
                pass
        assert not is_configured()
        handle = configure(role="daemon")
        handle.shutdown()
        print("OK")
    """)


def test_partial_exporter_build_is_recoverable(monkeypatch):
    from theater.observability import runtime

    class Resource:
        def __init__(self):
            self.shutdowns = 0

        def shutdown(self, timeout_millis=None):
            self.shutdowns += 1

    resource = Resource()
    calls = 0

    def build(_cls, _endpoint):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("build failed")
        return resource

    staged = runtime._StagedResources()
    monkeypatch.setattr(runtime, "_build_exporter", build)
    with pytest.raises(RuntimeError, match="build failed"):
        runtime._build_exporters("grpc", ("a", "b", "c"), staged)
    staged.rollback()
    assert resource.shutdowns == 1


def test_tracing_parentage():
    _run("""
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.trace import set_tracer_provider
        spans = []
        class _Cap:
            def export(self, s): spans.extend(s); return None
            def shutdown(self): pass
            def force_flush(self, timeout_millis=None): return True
        p = TracerProvider()
        p.add_span_processor(SimpleSpanProcessor(_Cap()))
        set_tracer_provider(p)
        from theater.observability.catalog import TraceKind
        from theater.observability.tracing import (
            start_span, detach_span, end_span, inject_trace_context,
        )
        parent, tok = start_span("parent", TraceKind.INTERNAL)
        child, ct = start_span("child", TraceKind.INTERNAL)
        carrier = inject_trace_context()
        assert "traceparent" in carrier
        detach_span(ct); end_span(child)
        detach_span(tok); end_span(parent)
        p.force_flush()
        c = [s for s in spans if s.name == "child"][0]
        par = [s for s in spans if s.name == "parent"][0]
        assert c.parent.span_id == par.context.span_id
        print("OK")
    """)


def test_extract_rejects_non_mapping():
    from theater.observability.tracing import extract_trace_context

    assert extract_trace_context(None) is None
    assert extract_trace_context("str") is None
    assert extract_trace_context({}) is None
