"""Pure OpenCode row, usage, timing, and fact-value helpers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from pathlib import Path

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness.base import SERVER_NAME, TokenUsage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    CostProvenance,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryUsage

from .constants import OPENCODE_MODEL_ID_KEY, OPENCODE_PROVIDER_ID_KEY, STEP_FINISH


def _seconds(ms) -> float | None:
    """Milliseconds to a unix epoch float. None for anything else."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    return ms / 1000.0


def load_json_object(raw: object) -> dict:
    """Decode a live JSON row to an object, failing open on malformed data."""
    if not isinstance(raw, (str, bytes)):
        return {}
    try:
        found = json.loads(raw)
    except ValueError:
        return {}
    return found if isinstance(found, dict) else {}


def _opencode_usage(info: dict) -> TokenUsage | None:
    """Extract usage from an OpenCode assistant message."""
    tokens = info.get("tokens")
    if not isinstance(tokens, dict):
        return None
    cache = tokens.get("cache") or {}
    cost = info.get("cost")
    # OpenCode uses zero when it has no per-turn price; zero falls through to model pricing.
    cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
    provider = info.get(OPENCODE_PROVIDER_ID_KEY)
    model_id = info.get(OPENCODE_MODEL_ID_KEY)
    if isinstance(provider, str) and isinstance(model_id, str) and provider and model_id:
        model = f"{provider}/{model_id}"
    elif isinstance(model_id, str) and model_id:
        model = model_id
    else:
        model = None
    native_id = info.get("id")
    usage_key = f"opencode:{native_id}" if isinstance(native_id, str) and native_id else None
    return TokenUsage(
        model=model,
        provider=provider if isinstance(provider, str) and provider else None,
        input_tokens=int(tokens.get("input") or 0),
        output_tokens=int(tokens.get("output") or 0),
        cache_creation_input_tokens=int(cache.get("write") or 0),
        cache_read_input_tokens=int(cache.get("read") or 0),
        reasoning_output_tokens=int(tokens.get("reasoning") or 0),
        cost_usd=cost,
        cost_provenance=(CostProvenance.REPORTED if cost is not None else CostProvenance.UNKNOWN),
        idempotency_key=usage_key,
    )


def _opencode_model(info: dict) -> str | None:
    model_data = _table(info.get("model"))
    provider = info.get(OPENCODE_PROVIDER_ID_KEY) or model_data.get(OPENCODE_PROVIDER_ID_KEY)
    model_id = (
        info.get(OPENCODE_MODEL_ID_KEY)
        or model_data.get(OPENCODE_MODEL_ID_KEY)
        or model_data.get("id")
    )
    if isinstance(provider, str) and isinstance(model_id, str) and provider and model_id:
        return f"{provider}/{model_id}"
    if isinstance(model_id, str) and model_id:
        return model_id
    return None


def _table(value) -> dict:
    """Return a nested object or an empty object."""
    return value if isinstance(value, dict) else {}


def _trajectory_identifier(value, prefix: str = "id") -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    if len(encoded) <= TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return value
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def _trajectory_text(value) -> str:
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", errors="replace").decode("utf-8")
        return value
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, default=str, sort_keys=True)
        except (TypeError, ValueError):
            return ""
    return ""


def _trajectory_string(value) -> str:
    return value if isinstance(value, str) else ""


def _opencode_mcp_identity(value: object) -> tuple[str, str] | None:
    prefix = f"{SERVER_NAME}_"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    tool = _trajectory_identifier(value.removeprefix(prefix), "tool")
    return (SERVER_NAME, tool) if tool is not None else None


def _trajectory_lane(kind: TrajectoryKind) -> TrajectoryLane:
    if kind is TrajectoryKind.USER:
        return TrajectoryLane.INPUT
    if kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT):
        return TrajectoryLane.TOOLS
    return TrajectoryLane.MODEL


def _trajectory_detail(
    name: str, value, *, format: ContentFormat = ContentFormat.TEXT
) -> DetailField | None:
    if value is None:
        return None
    text = _trajectory_text(value)
    if not text and isinstance(value, (int, float, bool)):
        text = json.dumps(value)
    if not text:
        return None
    try:
        return DetailField.from_text(name, text, format=format)
    except ValueError:
        return None


def _trajectory_seconds(value) -> float | None:
    found = _seconds(value)
    return found if found is not None and math.isfinite(found) else None


def _trajectory_timing(start, end, fallback=None) -> Timing | None:
    start_value = _trajectory_seconds(start)
    end_value = _trajectory_seconds(end)
    if start_value is None and end_value is None and fallback is not None:
        start_value = _trajectory_seconds(fallback)
    if start_value is None and end_value is None:
        return None
    if end_value is not None and start_value is not None and end_value < start_value:
        end_value = None
    duration = (
        (end_value - start_value) * 1000
        if start_value is not None and end_value is not None
        else None
    )
    try:
        return Timing(
            start=start_value,
            end=end_value,
            duration_ms=duration,
            provenance=TimingProvenance.SOURCE,
        )
    except ValueError:
        return None


def _message_timing(info: dict) -> Timing | None:
    time_data = _table(info.get("time"))
    return _trajectory_timing(time_data.get("created"), time_data.get("completed"))


def _part_timing(part: dict, fallback=None) -> Timing | None:
    state = _table(part.get("state"))
    time_data = _table(part.get("time")) or _table(state.get("time"))
    return _trajectory_timing(
        time_data.get("start") or time_data.get("created"),
        time_data.get("end") or time_data.get("completed"),
        fallback=fallback,
    )


def _trajectory_usage(info: dict) -> TrajectoryUsage | None:
    model = _trajectory_identifier(_opencode_model(info), "model")
    model_data = _table(info.get("model"))
    provider = _trajectory_identifier(
        info.get(OPENCODE_PROVIDER_ID_KEY) or model_data.get(OPENCODE_PROVIDER_ID_KEY),
        "provider",
    )
    try:
        usage = _opencode_usage(info)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return TrajectoryUsage(model=model, provider=provider) if model or provider else None
    if usage is None:
        return TrajectoryUsage(model=model, provider=provider) if model or provider else None
    values = {
        name: max(0, value) if isinstance(value, int) else 0
        for name, value in (
            ("input_tokens", usage.input_tokens),
            ("output_tokens", usage.output_tokens),
            ("reasoning_tokens", usage.reasoning_output_tokens),
            ("cache_read_tokens", usage.cache_read_input_tokens),
            ("cache_write_tokens", usage.cache_creation_input_tokens),
        )
    }
    cost = usage.cost_usd if usage.cost_usd is not None and math.isfinite(usage.cost_usd) else None
    if cost is not None and cost < 0:
        cost = None
    return TrajectoryUsage(
        model=model or _trajectory_identifier(usage.model, "model"),
        provider=provider or _trajectory_identifier(usage.provider, "provider"),
        request_id=_trajectory_identifier(usage.idempotency_key, "request"),
        cost_usd=cost,
        cost_provenance=(CostProvenance.REPORTED if cost is not None else CostProvenance.UNKNOWN),
        **values,
    )


def _assistant_request_id(usage: TrajectoryUsage | None, message_id: str) -> str | None:
    if usage is not None and usage.request_id is not None:
        return usage.request_id
    return _trajectory_identifier(f"opencode:{message_id}", "request") if message_id else None


def _opencode_source_key(db: Path) -> str:
    return hashlib.sha256(str(db.expanduser().resolve()).encode("utf-8")).hexdigest()[:32]


def _tool_output(state: dict) -> str:
    """What a finished tool call produced, or what went wrong."""
    output = state.get("output")
    if isinstance(output, str):
        return output
    error = state.get("error")
    if isinstance(error, str):
        return error
    if error is not None:
        return json.dumps(error, default=str)
    return "" if output is None else json.dumps(output, default=str)


def _stored_fact(
    *,
    kind: TrajectoryKind,
    summary: str,
    status: TrajectoryStatus,
    native_id: str | None,
    fallback_id: str | None,
    revision: int,
    raw_index: int,
    event_ordinal: int,
    turn_id: str | None = None,
    step_id: str | None = None,
    request_id: str | None = None,
    call_id: str | None = None,
    parent_call_id: str | None = None,
    mcp_server: str | None = None,
    mcp_tool: str | None = None,
    timing: Timing | None = None,
    usage: TrajectoryUsage | None = None,
    failure: TrajectoryFailure | None = None,
    details: Sequence[DetailField] = (),
) -> TrajectoryFact:
    native = _trajectory_identifier(native_id, "native") or _trajectory_identifier(
        fallback_id, "fallback"
    )
    return TrajectoryFact(
        kind=kind,
        lane=_trajectory_lane(kind),
        source="opencode",
        summary=summary,
        status=status,
        native_id=native,
        revision=max(0, revision),
        raw_index=max(0, raw_index),
        event_ordinal=max(0, event_ordinal),
        turn_id=_trajectory_identifier(turn_id, "turn"),
        step_id=_trajectory_identifier(step_id, "step"),
        request_id=_trajectory_identifier(request_id, "request"),
        call_id=_trajectory_identifier(call_id, "call"),
        parent_call_id=_trajectory_identifier(parent_call_id, "parent-call"),
        mcp_server=_trajectory_identifier(mcp_server, "mcp-server"),
        mcp_tool=_trajectory_identifier(mcp_tool, "mcp-tool"),
        timing=timing,
        usage=usage,
        failure=failure,
        details=tuple(details),
    )


def _finish_status(finish: object) -> TrajectoryStatus:
    if not finish:
        return TrajectoryStatus.RUNNING
    return TrajectoryStatus.PARTIAL if finish == STEP_FINISH else TrajectoryStatus.COMPLETED


def _tool_status(status: object) -> TrajectoryStatus:
    if status == "pending":
        return TrajectoryStatus.PENDING
    if status == "running":
        return TrajectoryStatus.RUNNING
    if status == "completed":
        return TrajectoryStatus.COMPLETED
    if status == "error":
        return TrajectoryStatus.ERROR
    return TrajectoryStatus.UNKNOWN
