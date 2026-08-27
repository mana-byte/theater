"""Strict bounded decoding for the supported inbound OTLP encodings."""

from __future__ import annotations

import base64
import importlib
import json
import math
from collections.abc import Mapping, Sequence

from theater.constants.harness import (
    HARNESS_OTEL_MAX_ATTRIBUTES,
    HARNESS_OTEL_MAX_RECORDS,
    HARNESS_OTEL_MAX_TEXT_BYTES,
    HARNESS_OTEL_MAX_VALUE_DEPTH,
)
from theater.harness.contracts.channels import OtelBounds, OtelRecord, OtelSignal


class OtelIngressError(ValueError):
    """An untrusted native OTel export exceeded its narrow contract."""


class OtelOptionalDependencyError(OtelIngressError):
    """An optional OTel decoding dependency is not installed."""


_DEFAULT_BOUNDS = OtelBounds(
    max_records=HARNESS_OTEL_MAX_RECORDS,
    max_attributes=HARNESS_OTEL_MAX_ATTRIBUTES,
    max_value_depth=HARNESS_OTEL_MAX_VALUE_DEPTH,
    max_text_bytes=HARNESS_OTEL_MAX_TEXT_BYTES,
)


def decode_otlp_json(
    body: bytes,
    *,
    bounds: OtelBounds = _DEFAULT_BOUNDS,
) -> tuple[OtelRecord, ...]:
    """Decode only the bounded OTLP HTTP JSON logs shape."""
    try:
        decoded = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OtelIngressError("native OTel body must be valid UTF-8") from exc
    try:
        payload = json.loads(decoded)
    except ValueError as exc:
        raise OtelIngressError("native OTel body must be valid JSON") from exc
    return _decode_logs_payload(payload, bounds=bounds)


def decode_otlp_protobuf(
    body: bytes,
    *,
    bounds: OtelBounds = _DEFAULT_BOUNDS,
) -> tuple[OtelRecord, ...]:
    """Lazily decode the optional OTLP protobuf logs request."""
    try:
        module = importlib.import_module(
            "opentelemetry.proto.collector.logs.v1.logs_service_pb2"
        )
        json_format = importlib.import_module("google.protobuf.json_format")
    except ImportError as exc:
        raise OtelOptionalDependencyError(
            "native OTel protobuf support requires the observability optional dependency"
        ) from exc
    request = module.ExportLogsServiceRequest()
    try:
        request.ParseFromString(body)
        payload = json_format.MessageToDict(request, use_integers_for_enums=True)
    except Exception as exc:
        raise OtelIngressError("native OTel protobuf body is malformed") from exc
    return _decode_logs_payload(payload, bounds=bounds)


def validate_records(records: Sequence[OtelRecord], bounds: OtelBounds) -> tuple[OtelRecord, ...]:
    """Apply one channel's narrower record limits after request decoding."""
    values = tuple(records)
    if len(values) > bounds.max_records:
        raise OtelIngressError("native OTel export has too many records")
    for record in values:
        _validate_mapping(record.resource, bounds, depth=0)
        _validate_mapping(record.attributes, bounds, depth=0)
        _validate_value(record.body, bounds, depth=0)
    return values


def _decode_logs_payload(payload: object, *, bounds: OtelBounds) -> tuple[OtelRecord, ...]:
    if not isinstance(payload, Mapping):
        raise OtelIngressError("native OTel body must be an object")
    resources = payload.get("resourceLogs")
    if not isinstance(resources, list):
        raise OtelIngressError("native OTel logs body must contain resourceLogs")
    records: list[OtelRecord] = []
    for resource_logs in resources:
        if not isinstance(resource_logs, Mapping):
            raise OtelIngressError("native OTel resourceLogs entries must be objects")
        resource = _attributes(resource_logs.get("resource", {}), bounds)
        scopes = resource_logs.get("scopeLogs")
        if not isinstance(scopes, list):
            raise OtelIngressError("native OTel resourceLogs entries must contain scopeLogs")
        for scope_logs in scopes:
            if not isinstance(scope_logs, Mapping):
                raise OtelIngressError("native OTel scopeLogs entries must be objects")
            values = scope_logs.get("logRecords")
            if not isinstance(values, list):
                raise OtelIngressError("native OTel scopeLogs entries must contain logRecords")
            for value in values:
                if len(records) >= bounds.max_records:
                    raise OtelIngressError("native OTel export has too many records")
                if not isinstance(value, Mapping):
                    raise OtelIngressError("native OTel log records must be objects")
                records.append(
                    OtelRecord(
                        signal=OtelSignal.LOGS,
                        resource=resource,
                        attributes=_attributes(value, bounds),
                        body=_value(value["body"], bounds, depth=0) if "body" in value else None,
                        timestamp_unix_nano=_uint(value.get("timeUnixNano"), "timestamp"),
                        observed_timestamp_unix_nano=_uint(
                            value.get("observedTimeUnixNano"), "observed timestamp"
                        ),
                        trace_id=_optional_text(value.get("traceId"), bounds, "trace id"),
                        span_id=_optional_text(value.get("spanId"), bounds, "span id"),
                        severity_number=_uint(value.get("severityNumber"), "severity number"),
                        severity_text=_optional_text(
                            value.get("severityText"),
                            bounds,
                            "severity text",
                        ),
                    )
                )
    return tuple(records)


def _attributes(value: object, bounds: OtelBounds) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise OtelIngressError("native OTel attributes owner must be an object")
    raw = value.get("attributes", [])
    if not isinstance(raw, list):
        raise OtelIngressError("native OTel attributes must be a list")
    if len(raw) > bounds.max_attributes:
        raise OtelIngressError("native OTel record has too many attributes")
    result: dict[str, object] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise OtelIngressError("native OTel attribute entries must be objects")
        key = item.get("key")
        if not isinstance(key, str):
            raise OtelIngressError("native OTel attribute keys must be strings")
        _text(key, bounds)
        if key in result:
            raise OtelIngressError("native OTel attribute keys must not repeat")
        if "value" not in item:
            raise OtelIngressError("native OTel attributes require a value")
        result[key] = _value(item["value"], bounds, depth=0)
    return result


def _value(value: object, bounds: OtelBounds, *, depth: int) -> object:  # noqa: PLR0912
    if depth > bounds.max_value_depth:
        raise OtelIngressError("native OTel value exceeds the maximum nesting depth")
    if not isinstance(value, Mapping) or len(value) != 1:
        raise OtelIngressError("native OTel values must contain one typed value")
    kind, raw = next(iter(value.items()))
    if kind == "stringValue":
        if not isinstance(raw, str):
            raise OtelIngressError("native OTel string values must be strings")
        _text(raw, bounds)
        return raw
    if kind == "boolValue":
        if type(raw) is not bool:
            raise OtelIngressError("native OTel bool values must be booleans")
        return raw
    if kind == "intValue":
        return _int64(raw)
    if kind == "doubleValue":
        if type(raw) not in (int, float) or not math.isfinite(float(raw)):
            raise OtelIngressError("native OTel double values must be finite numbers")
        return float(raw)
    if kind == "bytesValue":
        if not isinstance(raw, str):
            raise OtelIngressError("native OTel bytes values must be base64 strings")
        _text(raw, bounds)
        try:
            base64.b64decode(raw, validate=True)
        except ValueError as exc:
            raise OtelIngressError("native OTel bytes values must be valid base64") from exc
        return raw
    if kind == "arrayValue":
        if not isinstance(raw, Mapping) or not isinstance(raw.get("values"), list):
            raise OtelIngressError("native OTel array values must contain values")
        values = raw["values"]
        if len(values) > bounds.max_attributes:
            raise OtelIngressError("native OTel array values have too many entries")
        return tuple(_value(item, bounds, depth=depth + 1) for item in values)
    if kind == "kvlistValue":
        if not isinstance(raw, Mapping) or not isinstance(raw.get("values"), list):
            raise OtelIngressError("native OTel map values must contain values")
        values = raw["values"]
        if len(values) > bounds.max_attributes:
            raise OtelIngressError("native OTel map values have too many entries")
        result: dict[str, object] = {}
        for item in values:
            if not isinstance(item, Mapping) or not isinstance(item.get("key"), str):
                raise OtelIngressError("native OTel map entries require string keys")
            key = item["key"]
            _text(key, bounds)
            if key in result or "value" not in item:
                raise OtelIngressError("native OTel map entries must be unique key/value pairs")
            result[key] = _value(item["value"], bounds, depth=depth + 1)
        return result
    raise OtelIngressError("native OTel value type is unsupported")


def _validate_mapping(value: Mapping[str, object], bounds: OtelBounds, *, depth: int) -> None:
    if len(value) > bounds.max_attributes:
        raise OtelIngressError("native OTel record has too many attributes")
    for key, item in value.items():
        _text(key, bounds)
        _validate_value(item, bounds, depth=depth + 1)


def _validate_value(value: object, bounds: OtelBounds, *, depth: int) -> None:
    if value is None or type(value) in (bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise OtelIngressError("native OTel values must be finite")
        return
    if isinstance(value, str):
        _text(value, bounds)
        return
    if depth > bounds.max_value_depth:
        raise OtelIngressError("native OTel value exceeds the maximum nesting depth")
    if isinstance(value, tuple):
        if len(value) > bounds.max_attributes:
            raise OtelIngressError("native OTel array values have too many entries")
        for item in value:
            _validate_value(item, bounds, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        _validate_mapping(value, bounds, depth=depth + 1)
        return
    raise OtelIngressError("native OTel value type is unsupported")


def _text(value: str, bounds: OtelBounds) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise OtelIngressError("native OTel text must be valid UTF-8") from exc
    if size > bounds.max_text_bytes:
        raise OtelIngressError("native OTel text exceeds the channel limit")


def _uint(value: object, label: str) -> int | None:
    if value is None:
        return None
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and value.isdecimal():
        try:
            parsed = int(value)
        except ValueError as exc:
            raise OtelIngressError(f"native OTel {label} is out of range") from exc
    else:
        raise OtelIngressError(f"native OTel {label} must be an unsigned integer")
    if 0 <= parsed < 1 << 64:
        return parsed
    raise OtelIngressError(f"native OTel {label} is out of range")


def _int64(value: object) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and value and value.lstrip("-").isdigit():
        try:
            parsed = int(value)
        except ValueError as exc:
            raise OtelIngressError("native OTel integer value is out of range") from exc
    else:
        raise OtelIngressError("native OTel integer values must be integers")
    if -(1 << 63) <= parsed < 1 << 63:
        return parsed
    raise OtelIngressError("native OTel integer value is out of range")


def _optional_text(value: object, bounds: OtelBounds, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise OtelIngressError(f"native OTel {label} must be a non-blank string")
    _text(value, bounds)
    return value


__all__ = [
    "OtelIngressError",
    "OtelOptionalDependencyError",
    "decode_otlp_json",
    "decode_otlp_protobuf",
    "validate_records",
]
