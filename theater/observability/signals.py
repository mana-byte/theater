"""Direct structured OpenTelemetry log and completed-span transport."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


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
    ) -> None:
        if not self._active:
            return
        try:
            from opentelemetry._logs import SeverityNumber

            severity_name = {"WARNING": "WARN", "CRITICAL": "FATAL"}.get(
                severity_text.upper(), severity_text.upper()
            )
            severity_number = getattr(SeverityNumber, severity_name, SeverityNumber.INFO)
            self._logger.emit(
                timestamp=timestamp_ns,
                context=context,
                severity_number=severity_number,
                severity_text=severity_text,
                body=body,
                attributes=dict(attributes),
                event_name=event_name,
            )
        except Exception:
            return

    def emit_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, Any],
        start_time_ns: int | None = None,
        end_time_ns: int | None = None,
        parent_context: Any = None,
        links: Sequence[Any] = (),
        error: bool = False,
        error_type: str | None = None,
    ) -> Any | None:
        if not self._active:
            return None
        span: Any = None
        failed = False
        try:
            from opentelemetry.trace import Link, SpanKind, Status, StatusCode, set_span_in_context

            span = self._tracer.start_span(
                name,
                context=parent_context,
                kind=SpanKind.INTERNAL,
                attributes=dict(attributes),
                links=tuple(Link(link) for link in links),
                start_time=start_time_ns,
                record_exception=False,
                set_status_on_exception=False,
            )
            if error:
                span.set_status(Status(StatusCode.ERROR))
            if error_type:
                span.set_attribute("error.type", error_type)
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
