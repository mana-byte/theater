"""Direct structured OpenTelemetry log and completed-span transport."""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any

from theater.constants.observability import MAX_ERROR_TYPE_LEN
from theater.observability.catalog import TraceKind


class SignalBridge:
    """Transport bridge for agent-originated OpenTelemetry signals."""

    __slots__ = ("_active", "_logger", "_tracer")

    def __init__(self, logger: Any, tracer: Any) -> None:
        self._logger = logger
        self._tracer = tracer
        self._active = logger is not None and tracer is not None

    @property
    def active(self) -> bool:
        return self._active

    def deactivate(self) -> None:
        self._active = False

    def emit_log(
        self,
        event_name: str,
        *,
        body: Any,
        attributes: Mapping[str, Any],
        timestamp_ns: int | None = None,
        severity_text: str = "INFO",
        context: Any = None,
    ) -> bool:
        if not self._active:
            return False
        try:
            from opentelemetry._logs import SeverityNumber

            normalized_severity = severity_text.upper()
            severity_name = {"WARNING": "WARN", "CRITICAL": "FATAL"}.get(
                normalized_severity, normalized_severity
            )
            severity_number = getattr(SeverityNumber, severity_name, SeverityNumber.INFO)
            self._logger.emit(
                timestamp=timestamp_ns,
                observed_timestamp=time.time_ns(),
                context=context,
                severity_number=severity_number,
                severity_text=normalized_severity,
                body=body,
                attributes=dict(attributes),
                event_name=event_name,
            )
        except Exception:
            return False
        return True

    def emit_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any],
        start_time_ns: int | None = None,
        end_time_ns: int | None = None,
        parent_context: Any = None,
        links: Sequence[Any] = (),
        kind: TraceKind = TraceKind.INTERNAL,
        error: bool = False,
        error_type: str | None = None,
    ) -> Any | None:
        if not self._active:
            return None
        span: Any = None
        failed = False
        try:
            from opentelemetry.trace import Link, SpanKind, Status, StatusCode, set_span_in_context

            span_kind = {
                TraceKind.NONE: SpanKind.INTERNAL,
                TraceKind.INTERNAL: SpanKind.INTERNAL,
                TraceKind.CLIENT: SpanKind.CLIENT,
                TraceKind.SERVER: SpanKind.SERVER,
            }[kind]

            span = self._tracer.start_span(
                name,
                context=parent_context,
                kind=span_kind,
                attributes=dict(attributes),
                links=tuple(Link(link) for link in links),
                start_time=start_time_ns,
                record_exception=False,
                set_status_on_exception=False,
            )
            if error:
                span.set_status(Status(StatusCode.ERROR))
            if error_type:
                span.set_attribute("error.type", error_type[:MAX_ERROR_TYPE_LEN])
            context = set_span_in_context(span, parent_context)
        except Exception:
            failed = True
            context = None
        finally:
            if span is not None:
                try:
                    span.end(end_time=end_time_ns)
                except Exception:
                    failed = True
        return None if failed else context
