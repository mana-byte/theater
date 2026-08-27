from __future__ import annotations

import json
import math
from datetime import datetime

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import CostProvenance, TimingProvenance, TrajectoryStatus
from theater.trajectory.records import Timing, TrajectoryUsage

from .constants import CODEX_MODEL_PROVIDER_ID_KEY, CODEX_MODEL_PROVIDER_KEY


def _epoch(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _flatten(output) -> str:
    if isinstance(output, str):
        return output
    if not isinstance(output, list):
        return "" if output is None else json.dumps(output, default=str)
    parts = []
    for block in output:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


def _turn_id(payload: dict) -> str | None:
    """Return Codex's native boundary turn ID."""
    tid = payload.get("turn_id")
    return tid if isinstance(tid, str) and tid else None


def _safe_trajectory_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")
    return value


def _trajectory_id(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if len(encoded) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    return value


def _codex_mcp_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    invocation = value.get("invocation")
    if isinstance(invocation, dict):
        value = invocation
    server = _trajectory_id(value.get("server"))
    tool = _trajectory_id(value.get("tool"))
    return (server, tool) if server is not None and tool is not None else None


def _stable_json(value: object) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
        )
    except (TypeError, ValueError, UnicodeError):
        return json.dumps(str(value), ensure_ascii=True)


def _trajectory_detail(name: str, value: object, *, format: ContentFormat) -> DetailField:
    text = value if isinstance(value, str) else _stable_json(value)
    return DetailField.from_text(name, _safe_trajectory_text(text), format=format)


def _trajectory_int(value: object) -> int:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and math.isfinite(value) and value >= 0 and value.is_integer():
        return int(value)
    return 0


def _trajectory_float(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _trajectory_time(value: object) -> float | None:
    if isinstance(value, str):
        return _epoch(value)
    return _trajectory_float(value)


def _codex_duration(value: object) -> float | None:
    if isinstance(value, dict):
        seconds = _trajectory_float(value.get("secs"))
        nanos = _trajectory_float(value.get("nanos"))
        if seconds is not None and nanos is not None and seconds >= 0 and nanos >= 0:
            duration = seconds * 1000 + nanos / 1_000_000
            return duration if math.isfinite(duration) else None
        return None
    scalar_duration = _trajectory_float(value)
    return scalar_duration if scalar_duration is not None and scalar_duration >= 0 else None


def _codex_timing(record: dict, payload: dict, timestamp: float | None) -> Timing | None:
    values = (payload, record)
    start = next(
        (
            _trajectory_time(value.get(key))
            for value in values
            for key in ("started_at", "startedAt", "start_time", "startTime")
            if _trajectory_time(value.get(key)) is not None
        ),
        None,
    )
    end = next(
        (
            _trajectory_time(value.get(key))
            for value in values
            for key in ("completed_at", "completedAt", "end_time", "endTime")
            if _trajectory_time(value.get(key)) is not None
        ),
        None,
    )
    duration = next(
        (
            _codex_duration(value.get(key))
            for value in values
            for key in ("duration_ms", "durationMs", "duration")
            if _codex_duration(value.get(key)) is not None
        ),
        None,
    )
    first_token_ms = next(
        (
            _codex_duration(value.get(key))
            for value in values
            for key in ("time_to_first_token_ms", "timeToFirstTokenMs")
            if _codex_duration(value.get(key)) is not None
        ),
        None,
    )
    if start is None and end is None and duration is None and timestamp is not None:
        start = timestamp
    if start is None and end is None and duration is None:
        return None
    if start is not None and end is not None and end < start:
        end = None
    first_token = (
        start + first_token_ms / 1000 if start is not None and first_token_ms is not None else None
    )
    if first_token is not None and end is not None and first_token > end:
        first_token = None
    return Timing(
        start=start,
        end=end,
        first_token=first_token,
        duration_ms=duration,
        provenance=TimingProvenance.SOURCE,
    )


def _trajectory_status(value: object, default: TrajectoryStatus) -> TrajectoryStatus:
    if isinstance(value, TrajectoryStatus):
        return value
    if not isinstance(value, str):
        return default
    aliases = {
        "complete": TrajectoryStatus.COMPLETED,
        "completed": TrajectoryStatus.COMPLETED,
        "done": TrajectoryStatus.COMPLETED,
        "success": TrajectoryStatus.COMPLETED,
        "failed": TrajectoryStatus.ERROR,
        "failure": TrajectoryStatus.ERROR,
        "error": TrajectoryStatus.ERROR,
        "cancelled": TrajectoryStatus.CANCELLED,
        "canceled": TrajectoryStatus.CANCELLED,
        "aborted": TrajectoryStatus.INTERRUPTED,
        "interrupted": TrajectoryStatus.INTERRUPTED,
        "in_progress": TrajectoryStatus.RUNNING,
        "running": TrajectoryStatus.RUNNING,
        "partial": TrajectoryStatus.PARTIAL,
        "pending": TrajectoryStatus.PENDING,
    }
    return aliases.get(value.lower().replace("-", "_"), default)


def _codex_revision(record: dict, payload: dict) -> int:
    for value in (payload, record):
        for key in ("revision", "version"):
            candidate = _trajectory_int(value.get(key))
            if candidate or value.get(key) in (0, 0.0):
                return candidate
    return 0


def _codex_block_id(item_id: str | None, block: dict, ordinal: int) -> str | None:
    explicit = _trajectory_id(block.get("id"))
    if explicit is not None:
        return explicit
    if item_id is None:
        return None
    return item_id if ordinal == 0 else f"{item_id}:content:{ordinal}"


def _codex_scoped_id(value: str | None, suffix: str) -> str | None:
    return _trajectory_id(f"{value}:{suffix}") if value is not None else None


def _codex_content_text(value: object) -> str:
    if isinstance(value, str):
        return _safe_trajectory_text(value)
    if isinstance(value, list):
        text = "".join(
            _safe_trajectory_text(item.get("text"))
            for item in value
            if isinstance(item, dict) and isinstance(item.get("text"), str)
        )
        if text:
            return text
    return _safe_trajectory_text(_stable_json(value)) if value is not None else ""


def _codex_trajectory_turn_id(payload: dict) -> str | None:
    direct = _trajectory_id(payload.get("turn_id") or payload.get("turnId"))
    if direct is not None:
        return direct
    metadata = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(metadata, dict):
        return _trajectory_id(metadata.get("turn_id") or metadata.get("turnId"))
    return None


def _codex_usage(
    record: dict,
    payload: dict,
    *,
    model: str | None = None,
    provider: str | None = None,
    request_id: str | None = None,
) -> TrajectoryUsage | None:
    info = payload.get("info") if payload.get("type") == "token_count" else None
    raw = info.get("last_token_usage") if isinstance(info, dict) else None
    if not isinstance(raw, dict) and isinstance(info, dict):
        raw = info.get("total_token_usage")
    if not isinstance(raw, dict):
        raw = payload.get("usage") or payload.get("token_usage")
    if not isinstance(raw, dict):
        return None
    input_total = _trajectory_int(raw.get("input_tokens"))
    cache_read = _trajectory_int(raw.get("cached_input_tokens"))
    cache_write = _trajectory_int(raw.get("cache_write_input_tokens"))
    output_total = _trajectory_int(raw.get("output_tokens"))
    reasoning = _trajectory_int(raw.get("reasoning_output_tokens"))
    known = (
        input_total,
        cache_read,
        cache_write,
        output_total,
        reasoning,
    )
    if not any(known):
        return None
    model_value = None
    if isinstance(info, dict):
        model_value = info.get("model") or info.get("model_name")
    model_value = model_value or payload.get("model") or record.get("model") or model
    source_request_id = _trajectory_id(
        payload.get("request_id") or payload.get("requestId") or payload.get("turn_id")
    ) or _trajectory_id(request_id)
    cost = _trajectory_float(raw.get("cost_usd") if "cost_usd" in raw else raw.get("costUSD"))
    return TrajectoryUsage(
        model=_trajectory_id(model_value),
        provider=_trajectory_id(
            raw.get("provider")
            or raw.get(CODEX_MODEL_PROVIDER_KEY)
            or payload.get(CODEX_MODEL_PROVIDER_ID_KEY)
            or provider
        ),
        request_id=source_request_id,
        input_tokens=max(0, input_total - cache_read - cache_write),
        output_tokens=max(0, output_total - reasoning),
        reasoning_tokens=reasoning,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost if cost is None or cost >= 0 else None,
        cost_provenance=(
            CostProvenance.REPORTED if cost is not None and cost >= 0 else CostProvenance.UNKNOWN
        ),
    )
