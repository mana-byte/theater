"""Tracing: span lifecycle and W3C inject/extract."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from contextvars import Context as TaskContext
from contextvars import Token, copy_context
from typing import Any

from opentelemetry.context import Context
from opentelemetry.trace import Span, SpanKind, Tracer

from theater.observability.catalog import TraceKind


def background_context() -> TaskContext:
    """Preserve task context without extending a finished request's trace forever."""
    from opentelemetry import context as otel_context

    context = copy_context()
    context.run(otel_context.attach, otel_context.Context())
    return context


def _otel_span_kind(kind: TraceKind) -> SpanKind:
    return {
        TraceKind.NONE: SpanKind.INTERNAL,
        TraceKind.INTERNAL: SpanKind.INTERNAL,
        TraceKind.CLIENT: SpanKind.CLIENT,
        TraceKind.SERVER: SpanKind.SERVER,
    }[kind]


def _get_tracer() -> Tracer:
    from opentelemetry.trace import get_tracer

    return get_tracer("theater")


def start_span(
    name: str,
    kind: TraceKind,
    attributes: Mapping[str, Any] | None = None,
    parent_context: Context | None = None,
) -> tuple[Span | None, Token[Context] | None]:
    """Start OTel span, return (span, token). Ends span if context setup fails."""
    from opentelemetry import context as otel_context
    from opentelemetry.trace import set_span_in_context

    tracer = _get_tracer()
    span = tracer.start_span(
        name,
        context=parent_context,
        kind=_otel_span_kind(kind),
        attributes=dict(attributes) if attributes else None,
        record_exception=False,
        set_status_on_exception=False,
    )
    try:
        ctx = set_span_in_context(span, parent_context)
        token = otel_context.attach(ctx)
    except Exception:
        with contextlib.suppress(Exception):
            span.end()
        return None, None
    return span, token


def detach_span(token: Token[Context] | None) -> None:
    if token is None:
        return
    from opentelemetry import context as otel_context

    with contextlib.suppress(Exception):
        otel_context.detach(token)


def end_span(span: Span) -> None:
    with contextlib.suppress(Exception):
        span.end()


def set_span_status(span: Span, ok: bool) -> None:
    if ok:
        return
    from opentelemetry.trace import Status, StatusCode

    with contextlib.suppress(Exception):
        span.set_status(Status(StatusCode.ERROR))


def set_span_attributes(span: Span, attrs: Mapping[str, Any]) -> None:
    with contextlib.suppress(Exception):
        span.set_attributes(dict(attrs))


def record_error(span: Span, error_type: str) -> None:
    from theater.constants.observability import MAX_ERROR_TYPE_LEN

    if not error_type:
        return
    set_span_attributes(span, {"error.type": error_type[:MAX_ERROR_TYPE_LEN]})


def inject_trace_context() -> dict[str, str]:
    """Inject W3C trace context; empty when no valid current span."""
    from opentelemetry import context as otel_context
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    carrier: dict[str, str] = {}
    with contextlib.suppress(Exception):
        propagator = TraceContextTextMapPropagator()
        propagator.inject(carrier, context=otel_context.get_current())
    return carrier if carrier else {}


def extract_trace_context(carrier: Mapping[str, Any]) -> Context | None:
    """Extract W3C context; None for non-Mapping, empty, malformed, or invalid."""
    if not isinstance(carrier, Mapping) or not carrier:
        return None
    from opentelemetry.trace import get_current_span
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    try:
        propagator = TraceContextTextMapPropagator()
        ctx = propagator.extract(dict(carrier))
        span = get_current_span(ctx)
        span_ctx = span.get_span_context()
    except Exception:
        return None
    if span_ctx is None or not span_ctx.is_valid:
        return None
    return ctx
