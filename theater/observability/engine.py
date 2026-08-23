"""Timing engine: exact prose, log extras, metric bridge, outcome precedence."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import MutableMapping
from typing import Any, Literal

from theater.constants.observability import (
    DEFAULT_SLOW_MS,
    LAG_INTERVAL_S,
    LAG_WARN_S,
    MAX_ERROR_TYPE_LEN,
    READY_LAG_MAX_S,
)
from theater.observability.catalog import RESULTS, OperationSpec, _apply_transform
from theater.observability.metrics import MetricBridge

logger = logging.getLogger("theater.timing")
diagnostic_logger = logging.getLogger("theater.observability.engine")
_RESERVED_PROSE_FIELDS = frozenset({"error_type", "result"})

_bridge: MetricBridge | None = None


def set_metric_bridge(bridge: MetricBridge | None) -> None:
    global _bridge  # noqa: PLW0603
    _bridge = bridge


def metric_bridge() -> MetricBridge | None:
    return _bridge


def metric_bridge_active() -> bool:
    return _bridge is not None and _bridge.active


def _render(name: str, ms: float, fields: MutableMapping[str, Any]) -> str:
    tail = "".join(f" {key}={value}" for key, value in fields.items() if value is not None)
    return f"{name} {ms:.1f}ms{tail}"


def _error_type(exc: BaseException) -> str:
    try:
        code = getattr(exc, "code", None)
    except Exception:
        code = None
    if isinstance(code, str) and code:
        return code[:MAX_ERROR_TYPE_LEN]
    try:
        name = type(exc).__module__ + "." + type(exc).__qualname__
    except Exception:
        return ""
    return name[:MAX_ERROR_TYPE_LEN]


def _diagnose(signal: str) -> None:
    with contextlib.suppress(Exception):
        diagnostic_logger.debug("timing %s failed", signal, exc_info=True)


def _resolve_template(template: str, fields: dict[str, Any]) -> str:
    try:
        return template.format_map(_SafeDict(fields))
    except Exception:
        return template


class _SafeDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return key


def _build_metric_attrs(spec: OperationSpec, fields: dict[str, Any]) -> dict[str, Any] | None:
    if spec.metric_name is None:
        return None
    attrs: dict[str, Any] = dict(spec.static_attrs)
    for m in spec.attrs:
        if m.metric_key is None or m.source not in fields:
            continue
        value = _apply_transform(fields[m.source], m.metric_transform)
        if value is None:
            continue
        attrs[m.metric_key] = value
    return attrs


def _build_trace_attrs(spec: OperationSpec, fields: dict[str, Any]) -> dict[str, Any] | None:
    if spec.trace_template is None:
        return None
    attrs: dict[str, Any] = dict(spec.static_attrs)
    for m in spec.attrs:
        if m.trace_key is None or m.source not in fields:
            continue
        value = _apply_transform(fields[m.source], m.trace_transform)
        if value is None:
            continue
        attrs[m.trace_key] = value
    return attrs


def _build_prose_fields(spec: OperationSpec, fields: dict[str, Any]) -> dict[str, Any]:
    """Prose from raw fields; prose_key=None omits; template-only fields don't reappear."""
    if not spec.attrs:
        return {key: value for key, value in fields.items() if key not in _RESERVED_PROSE_FIELDS}
    result: dict[str, Any] = {}
    mappings = {mapping.source: mapping for mapping in spec.attrs}
    for key, raw_value in fields.items():
        if key in _RESERVED_PROSE_FIELDS:
            continue
        mapping = mappings.get(key)
        if mapping is None:
            result[key] = raw_value
            continue
        if mapping.prose_key is None:
            continue
        result[mapping.prose_key] = _apply_transform(raw_value, mapping.prose_transform)
    return result


def _build_log_extras(
    spec: OperationSpec,
    fields: dict[str, Any],
    duration_ms: float,
    result: str | None,
    error_type: str,
) -> dict[str, Any]:
    extras: dict[str, Any] = {
        "theater.operation": spec.key,
        "theater.duration_ms": duration_ms,
    }
    if spec.record_outcome and result is not None:
        extras["theater.result"] = result
    if error_type:
        extras["error.type"] = error_type
    for k, v in spec.static_attrs:
        extras[f"theater.{k}"] = v
    for m in spec.attrs:
        if m.otel_log_key is None:
            continue
        if m.source in fields and fields[m.source] is not None:
            extras[f"theater.{m.otel_log_key}"] = _apply_transform(
                fields[m.source], m.log_transform
            )
    return extras


class _SpanFields(dict):
    __slots__ = ("_ctx",)
    _ctx: _SpanContext

    def __init__(self, ctx: _SpanContext, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        object.__setattr__(self, "_ctx", ctx)

    def set_result(self, result: str, **attrs: Any) -> None:
        self._ctx._set_result(result, attrs)


class _SpanContext:
    __slots__ = (
        "_fields",
        "_metric_attrs",
        "_name",
        "_parent_context",
        "_prose_name",
        "_slow_ms",
        "_span",
        "_spec",
        "_started",
        "_synthetic_result",
        "_token",
        "_trace_attrs",
        "_trace_name",
    )

    def __init__(
        self,
        name: str | OperationSpec,
        *,
        slow_ms: float | None = None,
        parent_context: Any = None,
        **fields: Any,
    ) -> None:
        if isinstance(name, OperationSpec):
            self._spec: OperationSpec | None = name
            self._name = name.key
            self._slow_ms = slow_ms if slow_ms is not None else name.slow_ms
        else:
            self._spec = None
            self._name = name
            self._slow_ms = slow_ms if slow_ms is not None else DEFAULT_SLOW_MS
        self._parent_context = parent_context
        self._fields: _SpanFields = _SpanFields(self, fields)
        self._started: float | None = None
        self._metric_attrs: dict[str, Any] | None = None
        self._trace_attrs: dict[str, Any] | None = None
        self._prose_name: str | None = None
        self._trace_name: str | None = None
        self._span: Any = None
        self._token: Any = None
        self._synthetic_result: tuple[str, dict[str, Any]] | None = None

    def _set_result(self, result: str, attrs: dict[str, Any]) -> None:
        if result not in RESULTS:
            raise ValueError(f"result must be one of {RESULTS}, got {result!r}")
        self._synthetic_result = (result, attrs)

    def __enter__(self) -> _SpanFields:
        try:
            self._started = time.perf_counter()
        except Exception:
            _diagnose("clock start")
        spec = self._spec
        if spec is not None:
            try:
                self._metric_attrs = _build_metric_attrs(spec, self._fields)
            except Exception:
                _diagnose("metric attributes")
            try:
                self._trace_attrs = _build_trace_attrs(spec, self._fields)
            except Exception:
                _diagnose("trace attributes")
            try:
                if spec.log_template is not None:
                    self._prose_name = _resolve_template(spec.log_template, self._fields)
            except Exception:
                _diagnose("log name")
            try:
                if spec.trace_template is not None:
                    self._trace_name = _resolve_template(spec.trace_template, self._fields)
            except Exception:
                _diagnose("trace name")
            try:
                self._start_span()
            except Exception:
                _diagnose("span start")
        else:
            self._prose_name = self._name
        return self._fields

    def _start_span(self) -> None:
        spec = self._spec
        if spec is None or spec.trace_kind.value == "none" or spec.trace_template is None:
            return
        from theater.observability.tracing import start_span

        self._span, self._token = start_span(
            self._trace_name or spec.key,
            spec.trace_kind,
            attributes=self._trace_attrs,
            parent_context=self._parent_context,
        )

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        try:
            elapsed_ms = self._elapsed_ms()
            result, error_type = self._safe_outcome(exc_type, exc_val)
            self._finalize_observation(elapsed_ms, result, error_type)
        except Exception:
            _diagnose("finalization")
        finally:
            self._close_span()
        return False

    def _elapsed_ms(self) -> float | None:
        if self._started is not None:
            try:
                return (time.perf_counter() - self._started) * 1000.0
            except Exception:
                _diagnose("clock stop")
        return None

    def _safe_outcome(self, exc_type: Any, exc_val: Any) -> tuple[str, str]:
        try:
            return self._compute_outcome(exc_type, exc_val)
        except Exception:
            _diagnose("outcome")
            return ("error", "") if exc_type is not None else ("success", "")

    def _finalize_observation(self, elapsed_ms: float | None, result: str, error_type: str) -> None:
        spec = self._spec
        try:
            self._apply_outcome_attrs(spec, result, error_type)
        except Exception:
            _diagnose("outcome attributes")
        try:
            self._finalize_span(spec, result, error_type)
        except Exception:
            _diagnose("span finalize")
        if elapsed_ms is None:
            return
        try:
            self._finalize_metrics(spec, elapsed_ms)
        except Exception:
            _diagnose("metric record")
        try:
            self._finalize_log(spec, elapsed_ms, result, error_type)
        except Exception:
            _diagnose("timing log")

    def _close_span(self) -> None:
        token, current_span = self._token, self._span
        self._token = None
        self._span = None
        try:
            if token is not None:
                from theater.observability.tracing import detach_span

                detach_span(token)
        except Exception:
            _diagnose("span detach")
        try:
            if current_span is not None:
                from theater.observability.tracing import end_span

                end_span(current_span)
        except Exception:
            _diagnose("span end")

    def _compute_outcome(self, exc_type: Any, exc_val: Any) -> tuple[str, str]:
        if (
            exc_type is not None
            and isinstance(exc_type, type)
            and issubclass(exc_type, asyncio.CancelledError)
        ):
            return "cancelled", ""
        if exc_type is not None:
            return "error", _error_type(exc_val) if exc_val is not None else ""
        if self._synthetic_result is not None:
            result, extra_attrs = self._synthetic_result
            if result == "error":
                et = extra_attrs.get("error_type", "")
                if isinstance(et, str) and et:
                    return result, et[:MAX_ERROR_TYPE_LEN]
                return result, ""
            return result, ""
        return "success", ""

    def _apply_outcome_attrs(
        self, spec: OperationSpec | None, result: str, error_type: str
    ) -> None:
        if spec is None or not spec.record_outcome:
            return
        if self._metric_attrs is not None:
            self._metric_attrs["result"] = result
        if self._trace_attrs is not None:
            self._trace_attrs["result"] = result
            if error_type:
                self._trace_attrs["error.type"] = error_type

    def _finalize_span(self, spec: OperationSpec | None, result: str, error_type: str) -> None:
        if self._span is None:
            return
        from theater.observability.tracing import record_error, set_span_attributes, set_span_status

        set_span_status(self._span, result == "success")
        if error_type:
            record_error(self._span, error_type)
        if spec is not None and spec.record_outcome:
            set_span_attributes(self._span, {"result": result})

    def _finalize_metrics(self, spec: OperationSpec | None, elapsed_ms: float) -> None:
        if spec is None or spec.metric_name is None or _bridge is None:
            return
        _bridge.record(spec.metric_name, elapsed_ms, self._metric_attrs)

    def _finalize_log(
        self, spec: OperationSpec | None, elapsed_ms: float, result: str, error_type: str
    ) -> None:
        if self._prose_name is None:
            return
        if spec is not None:
            prose_fields = _build_prose_fields(spec, self._fields)
            extras = _build_log_extras(
                spec, self._fields, elapsed_ms, result if spec.record_outcome else None, error_type
            )
            rendered = _render(self._prose_name, elapsed_ms, prose_fields)
            if elapsed_ms >= self._slow_ms:
                logger.info("%s", rendered, extra=extras)
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug("%s", rendered, extra=extras)
        else:
            rendered = _render(self._prose_name, elapsed_ms, self._fields)
            if elapsed_ms >= self._slow_ms:
                logger.info("%s", rendered)
            elif logger.isEnabledFor(logging.DEBUG):
                logger.debug("%s", rendered)

    async def __aenter__(self) -> _SpanFields:
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        return self.__exit__(exc_type, exc_val, exc_tb)


def _emit_spec(name: OperationSpec, ms: float, *, slow_ms: float | None, **fields: Any) -> None:
    """Emit for OperationSpec: each signal guarded independently."""
    threshold = slow_ms if slow_ms is not None else name.slow_ms
    result = fields.get("result", "success")
    if result not in RESULTS:
        result = "success"
    error_type = fields.get("error_type", "")
    if not isinstance(error_type, str):
        error_type = ""
    error_type = error_type[:MAX_ERROR_TYPE_LEN]
    metric_attrs: dict[str, Any] | None = None
    try:
        metric_attrs = _build_metric_attrs(name, fields)
    except Exception:
        _diagnose("emit metric attributes")
    if name.record_outcome and metric_attrs is not None:
        metric_attrs["result"] = result
    if name.metric_name is not None and _bridge is not None:
        try:
            _bridge.record(name.metric_name, ms, metric_attrs)
        except Exception:
            _diagnose("emit metric record")
    if name.log_template is None:
        return
    try:
        prose_name = _resolve_template(name.log_template, fields)
        prose_fields = _build_prose_fields(name, fields)
    except Exception:
        _diagnose("emit prose")
        return
    try:
        extras = _build_log_extras(
            name,
            fields,
            ms,
            result if name.record_outcome else None,
            error_type,
        )
    except Exception:
        _diagnose("emit log attributes")
        extras = {}
    try:
        rendered = _render(prose_name, ms, prose_fields)
        if ms >= threshold:
            logger.info("%s", rendered, extra=extras)
        elif logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s", rendered, extra=extras)
    except Exception:
        _diagnose("emit log")


def emit(
    name: str | OperationSpec,
    ms: float,
    *,
    slow_ms: float | None = None,
    **fields: Any,
) -> None:
    """Log a duration; accept str or OperationSpec."""
    if isinstance(name, OperationSpec):
        _emit_spec(name, ms, slow_ms=slow_ms, **fields)
    else:
        threshold = slow_ms if slow_ms is not None else DEFAULT_SLOW_MS
        if ms >= threshold:
            logger.info("%s", _render(name, ms, fields))
        elif logger.isEnabledFor(logging.DEBUG):
            logger.debug("%s", _render(name, ms, fields))


def span(
    name: str | OperationSpec,
    *,
    slow_ms: float | None = None,
    parent_context: Any = None,
    **fields: Any,
) -> _SpanContext:
    """Time a block, including failures, and yield fields callers may extend."""
    return _SpanContext(name, slow_ms=slow_ms, parent_context=parent_context, **fields)


def ready_lag(
    name: str | OperationSpec,
    pid: str,
    created_at: float | None,
    **fields: Any,
) -> None:
    """Log a participant milestone measured across separate loops."""
    if created_at is None:
        return
    lag = time.time() - created_at
    if not 0.0 <= lag <= READY_LAG_MAX_S:
        return
    emit(name, lag * 1000.0, id=pid, **fields)


async def lag_monitor(stopping: asyncio.Event) -> None:
    """Warn when event-loop wake-up exceeds the lag budget."""
    loop = asyncio.get_running_loop()
    while not stopping.is_set():
        before = loop.time()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=LAG_INTERVAL_S)
        lag = loop.time() - before - LAG_INTERVAL_S
        if stopping.is_set():
            return
        clamped = max(0.0, lag)
        _record_event_loop_lag(clamped)
        if lag >= LAG_WARN_S:
            logger.warning(
                "event loop blocked for %.0fms — every agent's call and every "
                "observer poll waited that long; look for synchronous work "
                "(git, lsof, a large sweep) in the timing log just above",
                lag * 1000,
            )


def _record_event_loop_lag(lag_s: float) -> None:
    from theater.observability.catalog import EVENT_LOOP_LAG

    if _bridge is not None and EVENT_LOOP_LAG.metric_name is not None:
        with contextlib.suppress(Exception):
            _bridge.record(EVENT_LOOP_LAG.metric_name, lag_s * 1000.0)


def enable_trace() -> None:
    logger.setLevel(logging.DEBUG)
