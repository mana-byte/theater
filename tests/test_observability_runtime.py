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
]


def test_disabled_no_sdk():
    _run("""
        from theater.observability.runtime import configure, is_configured
        h = configure(role="daemon", otlp_enabled=False)
        assert not h.closed
        h.shutdown()
        assert h.closed and is_configured()
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


def test_views_built_from_catalog():
    """_build_views returns exponential aggregation views."""
    code = """
        from theater.observability.runtime import _build_views
        from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation
        views = _build_views()
        assert views
        assert any(isinstance(v._aggregation, ExponentialBucketHistogramAggregation) for v in views)
        print("OK")
    """
    r = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, check=False
    )
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert "OK" in r.stdout


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
