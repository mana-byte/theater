"""Direct OpenTelemetry signal bridge."""

from __future__ import annotations

from typing import Any


class _Logger:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def emit(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class _Tracer:
    def __init__(self) -> None:
        self.calls = 0

    def start_span(self, *args: Any, **kwargs: Any) -> None:
        self.calls += 1
        raise AssertionError("inactive bridge touched tracer")


def test_inactive_calls_do_not_touch_emitters() -> None:
    from theater.observability.signals import SignalBridge

    logger = _Logger()
    tracer = _Tracer()
    bridge = SignalBridge(logger, tracer)
    bridge.deactivate()

    bridge.emit_log("agent.event", body={"value": 1}, attributes={"kind": "test"})
    assert bridge.emit_span("agent.span", attributes={}) is None
    assert logger.calls == []
    assert tracer.calls == 0
    assert not bridge.active


def test_log_forwards_structured_event_and_copies_attributes() -> None:
    from opentelemetry._logs import SeverityNumber

    from theater.observability.signals import SignalBridge

    logger = _Logger()
    attrs = {"kind": "test"}
    context = object()
    SignalBridge(logger, object()).emit_log(
        "agent.event",
        body={"message": "hello"},
        attributes=attrs,
        timestamp_ns=123,
        severity_text="WARNING",
        context=context,
    )
    attrs["kind"] = "changed"

    assert logger.calls == [
        {
            "timestamp": 123,
            "context": context,
            "severity_number": SeverityNumber.WARN,
            "severity_text": "WARNING",
            "body": {"message": "hello"},
            "attributes": {"kind": "test"},
            "event_name": "agent.event",
        }
    ]


def test_completed_span_has_timestamps_error_and_parentable_context() -> None:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
    from opentelemetry.trace import get_current_span

    from theater.observability.signals import SignalBridge

    class Exporter:
        def __init__(self) -> None:
            self.spans: list[Any] = []

        def export(self, spans: list[Any]) -> SpanExportResult:
            self.spans.extend(spans)
            return SpanExportResult.SUCCESS

        def shutdown(self) -> None:
            return None

    exporter = Exporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    attributes = {"agent.kind": "delegate"}
    bridge = SignalBridge(_Logger(), tracer)

    context = bridge.emit_span(
        "agent.completed",
        attributes=attributes,
        start_time_ns=100,
        end_time_ns=200,
        error=True,
        error_type="agent.failure",
    )
    attributes["agent.kind"] = "changed"
    parent = get_current_span(context)
    child = tracer.start_span("agent.child", context=context)
    child.end(end_time=300)

    assert context is not None
    assert parent.get_span_context().is_valid
    completed = next(span for span in exporter.spans if span.name == "agent.completed")
    child_span = next(span for span in exporter.spans if span.name == "agent.child")
    assert completed.start_time == 100 and completed.end_time == 200
    assert completed.attributes == {"agent.kind": "delegate", "error.type": "agent.failure"}
    assert completed.status.status_code.name == "ERROR"
    assert child_span.parent is not None
    assert child_span.parent.span_id == completed.context.span_id
    provider.shutdown()


def test_emitter_and_span_failures_are_isolated() -> None:
    from opentelemetry.trace import INVALID_SPAN_CONTEXT

    from theater.observability.signals import SignalBridge

    class FailingLogger:
        def emit(self, **kwargs: Any) -> None:
            raise RuntimeError("export failed")

    class FailingTracer:
        def start_span(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("tracer failed")

    class FailingSpan:
        def __init__(self) -> None:
            self.ends = 0

        def get_span_context(self) -> Any:
            return INVALID_SPAN_CONTEXT

        def end(self, end_time: int | None = None) -> None:
            self.ends += 1
            raise RuntimeError("export failed")

    class EndFailingTracer:
        def __init__(self) -> None:
            self.span = FailingSpan()

        def start_span(self, *args: Any, **kwargs: Any) -> FailingSpan:
            return self.span

    SignalBridge(FailingLogger(), FailingTracer()).emit_log("agent.event", body={}, attributes={})
    assert SignalBridge(_Logger(), FailingTracer()).emit_span("agent.span", attributes={}) is None
    tracer = EndFailingTracer()
    assert SignalBridge(_Logger(), tracer).emit_span("agent.span", attributes={}) is None
    assert tracer.span.ends == 1
