"""Engine: exact prose, signal isolation, failure injection, outcome attrs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from functools import partial

import pytest

from tests.rig.tables import run_rows
from theater import timing
from theater.observability.catalog import BY_KEY
from theater.observability.engine import span

TIMING = "theater.timing"


def _msgs(caplog, level=logging.INFO):
    return [r.message for r in caplog.records if r.levelno == level]


def _norm(s):
    return re.sub(r"\d+\.\d+ms", "0.0ms", s)


def _line(caplog):
    return _norm(_msgs(caplog)[0])


# --- Exact prose table ---


def test_exact_prose(caplog):
    """Every instrumented operation renders its documented prose, verbatim."""

    def check(key: str, fields: dict, expected: str) -> None:
        caplog.clear()
        caplog.set_level(logging.DEBUG, logger=TIMING)
        with span(BY_KEY[key], slow_ms=0.0, **{k: v for k, v in fields.items() if k != "rc"}) as sp:
            if "rc" in fields:
                sp["rc"] = fields["rc"]
            if key == "KILL_PANE":
                sp["attempts"] = 3
        assert _line(caplog) == expected

    run_rows(
        (key, partial(check, key, fields, expected))
        for key, fields, expected in [
            (
                "LIFECYCLE_STAGE",
                {"action": "kill", "stage": "presence", "id": "abc", "operation_id": "op1"},
                "kill.presence 0.0ms id=abc operation=op1",
            ),
            ("PROC_PS_COMM", {"pid": 12345}, "proc.ps-comm 0.0ms pid=12345"),
            ("PROC_LSOF", {"pid": 99}, "proc.lsof 0.0ms pid=99"),
            ("TMUX_COMMAND", {"command": "list-panes"}, "tmux.list-panes 0.0ms"),
            (
                "GIT_COMMAND",
                {"command": "worktree-add", "cwd": "/tmp", "rc": 0},
                "git.worktree-add 0.0ms cwd=/tmp rc=0",
            ),
            ("SPAWN_WORKTREE", {"id": "abc", "kind": None}, "spawn.worktree 0.0ms id=abc"),
            (
                "SPAWN_LAUNCH",
                {"id": "abc", "harness": "vibe"},
                "spawn.terminal 0.0ms id=abc harness=vibe",
            ),
            (
                "KILL_PANE",
                {"id": "abc", "pane": "%5", "harness": "vibe"},
                "kill.pane 0.0ms id=abc pane=%5 attempts=3",
            ),
            ("KILL_TEARDOWN", {"id": "abc", "harness": "vibe"}, "kill.teardown 0.0ms id=abc"),
            ("RPC_SERVER", {"method": "spawn", "caller": "x"}, "rpc.spawn 0.0ms caller=x"),
        ]
    )


def test_no_result_in_prose(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    with span(BY_KEY["PROC_PS_TABLE"], slow_ms=0.0):
        pass
    assert "result" not in _line(caplog)


def test_emit_outcome_fields_do_not_enter_prose(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    timing.emit(
        BY_KEY["PROC_PS_TABLE"],
        1.0,
        slow_ms=0.0,
        result="error",
        error_type="broken",
    )
    assert _line(caplog) == "proc.ps-table 0.0ms"


def test_prose_keeps_caller_field_order(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    with span(BY_KEY["GIT_COMMAND"], slow_ms=0.0, rc=1, command="status", cwd="/tmp"):
        pass
    assert _line(caplog) == "git.status 0.0ms rc=1 cwd=/tmp"


def test_rpc_client_log_includes_outcome(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    with span(BY_KEY["RPC_CLIENT"], slow_ms=0.0, method="spawn"):
        pass
    assert _line(caplog) == "rpc.client spawn 0.0ms result=success"
    caplog.clear()
    timing.emit(BY_KEY["RPC_CLIENT"], 1.0, slow_ms=0.0, method="spawn", result="error")
    assert _line(caplog) == "rpc.client spawn 0.0ms result=error"


def test_disabled_timing_log_does_not_render_fields(monkeypatch, caplog):
    from theater.observability import engine

    caplog.set_level(logging.WARNING, logger=TIMING)
    monkeypatch.setattr(engine, "_build_prose_fields", lambda *_, **__: pytest.fail("rendered"))
    with span(BY_KEY["RPC_CLIENT"], method="ping"):
        pass
    timing.emit(BY_KEY["RPC_CLIENT"], 1.0, method="ping")


def test_late_fields_and_subphase_reach_trace_and_log(monkeypatch, caplog):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from theater.observability import engine, tracing

    caplog.set_level(logging.DEBUG, logger=TIMING)
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_get_tracer", lambda: provider.get_tracer("test"))
    clock = iter((1.0, 1.1, 1.125, 1.2))
    monkeypatch.setattr(engine.time, "perf_counter", lambda: next(clock))
    try:
        with span(BY_KEY["RPC_CLIENT"], method="ping") as fields, fields.measure("connect_ms"):
            fields["request_id"] = 7
        (recorded,) = exporter.get_finished_spans()
        assert recorded.attributes["theater.connect_ms"] == 25.0
        assert recorded.attributes["theater.request_id"] == 7
        assert recorded.attributes["result"] == "success"
        assert caplog.records[-1].__dict__["theater.connect_ms"] == 25.0
    finally:
        provider.shutdown()


@pytest.mark.parametrize("wall_elapsed", [0.25, 600.25, -100.0])
def test_clock_discontinuities_are_annotated_without_rewriting_span_time(
    monkeypatch, caplog, wall_elapsed
):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from theater.observability import engine, tracing

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_get_tracer", lambda: provider.get_tracer("test"))
    caplog.set_level(logging.DEBUG, logger=TIMING)
    try:
        with monkeypatch.context() as clocks:
            clocks.setattr(engine.time, "perf_counter", lambda: 1.0)
            clocks.setattr(engine.time, "time", lambda: 1000.0)
            with span(BY_KEY["PROC_PS_TABLE"]):
                clocks.setattr(engine.time, "perf_counter", lambda: 1.25)
                clocks.setattr(engine.time, "time", lambda: 1000.0 + wall_elapsed)
        (recorded,) = exporter.get_finished_spans()
        assert recorded.attributes["theater.duration_ms"] == 250.0
        assert recorded.attributes["theater.wall_duration_ms"] == wall_elapsed * 1000
        assert recorded.attributes["theater.clock_discontinuity"] is (wall_elapsed != 0.25)
        assert recorded.end_time - recorded.start_time < 1_000_000_000
        assert caplog.records[-1].__dict__["theater.clock_gap_ms"] == wall_elapsed * 1000 - 250
    finally:
        provider.shutdown()


def test_observer_attach_prose(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    from theater.models import now as wall_now

    timing.ready_lag(BY_KEY["OBSERVER_ATTACH"], "pid1", wall_now() - 2.0, harness="codex")
    assert _norm(_msgs(caplog)[0]).startswith("observer.attach 0.0ms id=pid1 harness=codex")


# --- Failure injection ---


class _SpyBridge:
    active = True

    def __init__(self):
        self.recorded = []

    def record(self, name, value, attrs):
        self.recorded.append((name, attrs))


def test_metric_failure_keeps_log(monkeypatch, caplog):
    from theater.observability import engine

    calls = []
    caplog.set_level(logging.INFO, logger=TIMING)

    class _Boom:
        active = True

        def record(self, *a, **kw):
            calls.append("metric")
            raise RuntimeError("broke")

    monkeypatch.setattr(engine, "_bridge", _Boom())
    monkeypatch.setattr(logging.getLogger(TIMING), "info", lambda *a, **kw: calls.append("log"))
    with span(BY_KEY["PROC_PS_TABLE"], slow_ms=0.0):
        pass
    assert "metric" in calls and "log" in calls


def test_log_failure_keeps_app_exception(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("log broke")

    monkeypatch.setattr(logging.getLogger(TIMING), "info", boom)
    with (
        pytest.raises(ValueError, match="app error"),
        span(BY_KEY["PROC_PS_TABLE"], slow_ms=0.0),
    ):
        raise ValueError("app error")


def test_metric_attribute_failure_keeps_log(monkeypatch, caplog):
    from theater.observability import engine

    caplog.set_level(logging.INFO, logger=TIMING)
    monkeypatch.setattr(engine, "_build_metric_attrs", lambda *_: 1 / 0)
    seen = []
    monkeypatch.setattr(logging.getLogger(TIMING), "info", lambda *a, **kw: seen.append(a[1]))
    with span(BY_KEY["PROC_PS_TABLE"], slow_ms=0.0):
        pass
    assert seen and seen[0].startswith("proc.ps-table ")


def test_trace_attach_failure_ends_span_once(monkeypatch):
    from opentelemetry import context as otel_context

    from theater.observability import tracing
    from theater.observability.catalog import TraceKind

    class FakeSpan:
        def __init__(self):
            self.ends = 0

        def end(self):
            self.ends += 1

    fake_span = FakeSpan()

    class FakeTracer:
        def start_span(self, *args, **kwargs):
            return fake_span

    monkeypatch.setattr(tracing, "_get_tracer", FakeTracer)
    monkeypatch.setattr(otel_context, "attach", lambda _context: 1 / 0)
    current_span, token = tracing.start_span("x", TraceKind.INTERNAL)
    assert current_span is None and token is None and fake_span.ends == 1


def test_exit_returns_false():
    ctx = span("t", slow_ms=0.0)
    ctx.__enter__()
    assert ctx.__exit__(ValueError, ValueError("x"), None) is False


# --- Outcome attrs inspected ---


@pytest.mark.parametrize(
    "exc,result",
    [
        (None, "success"),
        (ValueError("boom"), "error"),
        (asyncio.CancelledError(), "cancelled"),
    ],
)
def test_outcome_result_in_metric(monkeypatch, exc, result):
    from theater.observability import engine

    spy = _SpyBridge()
    monkeypatch.setattr(engine, "_bridge", spy)
    if exc is not None:
        with pytest.raises(type(exc)), span(BY_KEY["PROC_PS_TABLE"], slow_ms=0.0):
            raise exc
    else:
        with span(BY_KEY["PROC_PS_TABLE"], slow_ms=0.0):
            pass
    assert spy.recorded[0][1]["result"] == result


def test_log_extras_error_dot_type(monkeypatch, caplog):
    from theater.observability import engine

    caplog.set_level(logging.INFO, logger=TIMING)
    monkeypatch.setattr(engine, "_bridge", None)
    extras = {}
    monkeypatch.setattr(
        logging.getLogger(TIMING), "info", lambda *a, **kw: extras.update(kw.get("extra", {}))
    )
    with pytest.raises(ValueError, match="boom"), span(BY_KEY["PROC_PS_TABLE"], slow_ms=0.0):
        raise ValueError("boom")
    assert "error.type" in extras and "theater.error_type" not in extras


def test_synthetic_none_error_type_not_exported(monkeypatch):
    from theater.observability import engine

    spy = _SpyBridge()
    monkeypatch.setattr(engine, "_bridge", spy)
    with span(BY_KEY["GIT_COMMAND"], slow_ms=0.0, command="status") as sp:
        sp.set_result("error")
    assert spy.recorded[0][1]["result"] == "error"


def test_set_result_validates():
    with (
        span(BY_KEY["PROC_PS_TABLE"], slow_ms=0.0) as fields,
        pytest.raises(ValueError, match="result must be one of"),
    ):
        fields.set_result("bogus")


# --- emit / ready_lag ---


def test_emit_spec_no_log_template_none(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)
    timing.emit(BY_KEY["EVENT_LOOP_LAG"], 5.0)
    assert caplog.records == []


def test_emit_metric_only_spec_records(monkeypatch):
    from theater.observability import engine

    spy = _SpyBridge()
    monkeypatch.setattr(engine, "_bridge", spy)
    timing.emit(BY_KEY["EVENT_LOOP_LAG"], 5.0)
    assert spy.recorded == [("theater.eventloop.lag", {})]


def test_lag_monitor_no_debug(caplog):
    caplog.set_level(logging.DEBUG, logger=TIMING)

    async def run():
        stopping = asyncio.Event()
        task = asyncio.create_task(timing.lag_monitor(stopping))
        await asyncio.sleep(0.1)
        stopping.set()
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert not any("eventloop" in r.message for r in caplog.records if r.levelno == logging.DEBUG)
