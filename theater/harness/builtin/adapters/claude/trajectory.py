"""Claude timing, usage, and trajectory projection."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from theater.constants.trajectory import (
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_MCP_CALL_CONTEXT_LIMIT,
    TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES,
)
from theater.harness.base import NativeChild, TokenUsage
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    CostProvenance,
    TimingProvenance,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryUsage


@dataclass(frozen=True, slots=True)
class _ClaudeCausalRecord:
    timestamp: float | None
    turn_id: str | None


@dataclass(frozen=True, slots=True)
class _ClaudeRequestClock:
    start: float | None
    first_token: float | None


@dataclass(frozen=True, slots=True)
class _ClaudeTimingProjection:
    record: Timing | None
    request: Timing | None
    turn_id: str | None


def _epoch(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _token_usage(message: dict, record: dict) -> TokenUsage | None:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    model = message.get("model")
    if not isinstance(model, str) or not model:
        model = None
    cost = record.get("costUSD")
    cost = float(cost) if isinstance(cost, (int, float)) and cost > 0 else None
    provider = message.get("provider") or record.get("provider")
    native_id = message.get("id") or record.get("requestId")
    usage_key = f"claude:{native_id}" if isinstance(native_id, str) and native_id else None
    return TokenUsage(
        model=model,
        provider=provider if isinstance(provider, str) and provider else None,
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_creation_input_tokens=int(raw.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(raw.get("cache_read_input_tokens") or 0),
        cost_usd=cost,
        cost_provenance=(CostProvenance.REPORTED if cost is not None else CostProvenance.UNKNOWN),
        idempotency_key=usage_key,
    )


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
        value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value):
        return None
    if len(value.encode("utf-8")) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
        return None
    return value


def _claude_mcp_identity(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str) or not value.startswith("mcp__"):
        return None
    server, separator, tool = value.removeprefix("mcp__").partition("__")
    if not separator:
        return None
    server_id = _trajectory_id(server)
    tool_id = _trajectory_id(tool)
    return (server_id, tool_id) if server_id is not None and tool_id is not None else None


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
    return _epoch(value) if isinstance(value, str) else _trajectory_float(value)


def _trajectory_duration(record: dict) -> float | None:
    for key in ("durationMs", "duration_ms"):
        value = _trajectory_float(record.get(key))
        if value is not None and value >= 0:
            return value
    return None


def _claude_timing(record: dict, timestamp: float | None) -> Timing | None:
    start = next(
        (
            _trajectory_time(record.get(key))
            for key in ("startTimestamp", "start_timestamp", "startedAt", "started_at")
            if _trajectory_time(record.get(key)) is not None
        ),
        None,
    )
    end = next(
        (
            _trajectory_time(record.get(key))
            for key in ("endTimestamp", "end_timestamp", "completedAt", "completed_at")
            if _trajectory_time(record.get(key)) is not None
        ),
        None,
    )
    if start is None and timestamp is not None:
        start = timestamp
    duration = _trajectory_duration(record)
    if start is None and end is None and duration is None:
        return None
    if start is not None and end is not None and end < start:
        end = None
    return Timing(start=start, end=end, duration_ms=duration, provenance=TimingProvenance.SOURCE)


def _claude_turn_timing(record: dict, timestamp: float | None) -> Timing | None:
    explicit = _claude_timing(record, None)
    start = explicit.start if explicit is not None else None
    end = explicit.end if explicit is not None else None
    duration = explicit.duration_ms if explicit is not None else None
    end = end if end is not None else timestamp
    derived = False
    if duration is not None:
        if start is None and end is not None:
            start = end - duration / 1_000
            derived = True
        elif end is None and start is not None:
            end = start + duration / 1_000
            derived = True
    elif start is not None and end is not None:
        duration = (end - start) * 1_000
        derived = True
    if start is not None and end is not None and end < start:
        start = None
    if start is None and end is None and duration is None:
        return None
    return Timing(
        start=start,
        end=end,
        duration_ms=duration,
        provenance=TimingProvenance.DERIVED if derived else TimingProvenance.SOURCE,
    )


def _claude_request_id(message: dict, record: dict) -> str | None:
    return _trajectory_id(
        record.get("requestId")
        or record.get("request_id")
        or message.get("requestId")
        or message.get("request_id")
        or message.get("id")
    )


def _remember_bounded[ContextValue](
    mapping: dict[str, ContextValue], key: str, value: ContextValue
) -> None:
    mapping.pop(key, None)
    mapping[key] = value
    while len(mapping) > TRAJECTORY_MCP_CALL_CONTEXT_LIMIT:
        mapping.pop(next(iter(mapping)))


def _claude_request_bounds(
    explicit: Timing | None,
    prior: _ClaudeRequestClock | None,
    timestamp: float | None,
    parent_timestamp: float | None,
) -> tuple[float | None, float | None, float | None]:
    start = explicit.start if explicit is not None else None
    end = explicit.end if explicit is not None else None
    duration = explicit.duration_ms if explicit is not None else None
    if duration is not None:
        if start is not None and end is None:
            end = start + duration / 1_000
        elif end is not None and start is None:
            start = end - duration / 1_000
        elif start is None and end is None and timestamp is not None:
            end = timestamp
            start = end - duration / 1_000
    if start is None and prior is not None:
        start = prior.start
    if start is None:
        start = parent_timestamp
    if end is None:
        end = timestamp
    if start is not None and end is not None and end < start:
        start = end = None
    return start, end, duration


def _claude_first_token(
    prior: _ClaudeRequestClock | None,
    timestamp: float | None,
    start: float | None,
    end: float | None,
) -> float | None:
    first_token = prior.first_token if prior is not None else timestamp
    if first_token is not None and start is not None and first_token < start:
        return None
    if first_token is not None and end is not None and first_token > end:
        return None
    return first_token


def _claude_request_timing_value(
    explicit: Timing | None,
    fallback: Timing | None,
    start: float | None,
    end: float | None,
    duration: float | None,
    first_token: float | None,
) -> Timing | None:
    if start is not None and end is not None:
        complete_source = (
            explicit is not None
            and explicit.start is not None
            and explicit.end is not None
            and explicit.duration_ms is not None
        )
        return Timing(
            start=start,
            end=end,
            duration_ms=duration if duration is not None else (end - start) * 1_000,
            provenance=(TimingProvenance.SOURCE if complete_source else TimingProvenance.DERIVED),
            first_token=first_token,
        )
    if duration is None:
        return fallback
    return Timing(
        start=start,
        end=end,
        duration_ms=duration,
        provenance=explicit.provenance if explicit is not None else TimingProvenance.SOURCE,
        first_token=first_token,
    )


def _trajectory_status(value: object, default: TrajectoryStatus) -> TrajectoryStatus:
    if isinstance(value, TrajectoryStatus):
        return value
    if not isinstance(value, str):
        return default
    normalized = value.lower().replace("-", "_")
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
        "in_progress": TrajectoryStatus.RUNNING,
        "running": TrajectoryStatus.RUNNING,
        "partial": TrajectoryStatus.PARTIAL,
        "interrupted": TrajectoryStatus.INTERRUPTED,
        "pending": TrajectoryStatus.PENDING,
    }
    return aliases.get(normalized, default)


def _claude_trajectory_usage(message: dict, record: dict) -> TrajectoryUsage | None:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    model = _trajectory_id(message.get("model"))
    provider = _trajectory_id(message.get("provider") or record.get("provider"))
    request_id = _claude_request_id(message, record)
    cost = _trajectory_float(record.get("costUSD"))
    return TrajectoryUsage(
        model=model,
        provider=provider,
        request_id=request_id,
        input_tokens=_trajectory_int(raw.get("input_tokens")),
        output_tokens=_trajectory_int(raw.get("output_tokens")),
        reasoning_tokens=_trajectory_int(raw.get("reasoning_output_tokens")),
        cache_read_tokens=_trajectory_int(raw.get("cache_read_input_tokens")),
        cache_write_tokens=_trajectory_int(raw.get("cache_creation_input_tokens")),
        cost_usd=cost if cost is None or cost >= 0 else None,
        cost_provenance=(
            CostProvenance.REPORTED if cost is not None and cost >= 0 else CostProvenance.UNKNOWN
        ),
    )


def _claude_revision(record: dict) -> int:
    message = record.get("message")
    values = [record, message] if isinstance(message, dict) else [record]
    for value in values:
        for key in ("revision", "version"):
            candidate = _trajectory_int(value.get(key))
            if candidate or value.get(key) in (0, 0.0):
                return candidate
    return 0


def _claude_block_native_id(
    block: dict, base_id: str | None, record_id: str | None, ordinal: int
) -> str | None:
    explicit = _trajectory_id(block.get("id"))
    if explicit is not None:
        return explicit
    if record_id is not None:
        return record_id if ordinal == 0 else f"{record_id}:block:{ordinal}"
    if base_id is not None:
        return base_id if ordinal == 0 else f"{base_id}:block:{ordinal}"
    return None


def _claude_content_text(value: object) -> str:
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


def _relativise(path: str, cwd: str | None) -> str | None:
    if not path:
        return None
    if not path.startswith("/"):
        return path
    if cwd is None:
        return None
    c = cwd.rstrip("/") + "/"
    if not (path == cwd or path.startswith(c)):
        return None
    return "." if path == cwd else path[len(c) :]


class ClaudeTrajectory:
    _mcp_calls: dict[str, tuple[str, str]]
    _causal_records: dict[str, _ClaudeCausalRecord]
    _request_clocks: dict[str, _ClaudeRequestClock]

    def _trajectory_timing(self, record: dict, timestamp: float | None) -> _ClaudeTimingProjection:
        parent_id = _trajectory_id(record.get("parentUuid") or record.get("parent_uuid"))
        parent = self._causal_records.get(parent_id) if parent_id is not None else None
        turn_id = _trajectory_id(
            record.get("turn_id") or record.get("turnId") or record.get("promptId")
        )
        if turn_id is None and parent is not None:
            turn_id = parent.turn_id

        is_turn_duration = (
            record.get("type") == "system" and record.get("subtype") == "turn_duration"
        )
        record_timing = (
            _claude_turn_timing(record, timestamp)
            if is_turn_duration
            else _claude_timing(record, timestamp)
        )
        request_timing: Timing | None = None
        if record.get("type") == "assistant":
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            request_id = _claude_request_id(message, record)
            if request_id is not None:
                request_timing = self._request_timing(
                    request_id,
                    record,
                    timestamp,
                    parent.timestamp if parent is not None else None,
                    record_timing,
                )

        record_id = _trajectory_id(record.get("uuid") or record.get("id"))
        if record_id is not None:
            anchor = timestamp
            if anchor is None and record_timing is not None:
                anchor = record_timing.end if record_timing.end is not None else record_timing.start
            if anchor is None and parent is not None:
                anchor = parent.timestamp
            _remember_bounded(self._causal_records, record_id, _ClaudeCausalRecord(anchor, turn_id))
        return _ClaudeTimingProjection(record_timing, request_timing, turn_id)

    def _request_timing(
        self,
        request_id: str,
        record: dict,
        timestamp: float | None,
        parent_timestamp: float | None,
        record_timing: Timing | None,
    ) -> Timing | None:
        prior = self._request_clocks.get(request_id)
        explicit = _claude_timing(record, None)
        start, end, duration = _claude_request_bounds(explicit, prior, timestamp, parent_timestamp)
        first_token = _claude_first_token(prior, timestamp, start, end)
        request_timing = _claude_request_timing_value(
            explicit, record_timing, start, end, duration, first_token
        )
        clock_start = start
        if clock_start is None and prior is not None:
            clock_start = prior.start
        if clock_start is None:
            clock_start = timestamp
        _remember_bounded(
            self._request_clocks,
            request_id,
            _ClaudeRequestClock(clock_start, first_token),
        )
        return request_timing

    def _remember_mcp_calls(self, record: dict) -> None:
        if record.get("type") != "assistant":
            return
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {
                "tool_use",
                "server_tool_use",
            }:
                continue
            call_id = _trajectory_id(block.get("id") or block.get("call_id"))
            identity = _claude_mcp_identity(block.get("name"))
            if call_id is None or identity is None:
                continue
            _remember_bounded(self._mcp_calls, call_id, identity)

    def _seed_mcp_context(self, fh: BinaryIO, start: int) -> None:
        self._mcp_calls.clear()
        self._causal_records.clear()
        self._request_clocks.clear()
        scan_start = max(0, start - TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES)
        fh.seek(scan_start)
        context = fh.read(start - scan_start)
        if scan_start:
            _, separator, context = context.partition(b"\n")
            if not separator:
                return
        for raw in context.splitlines():
            record = self._decode(raw.decode("utf-8", errors="replace"))  # type: ignore[attr-defined]
            if record is not None:
                self._remember_mcp_calls(record)
                self._trajectory_timing(record, _epoch(record.get("timestamp")))

    def _trajectory_facts(  # noqa: PLR0912, PLR0915
        self, record: dict, index: int
    ) -> list[TrajectoryFact]:
        timestamp = _epoch(record.get("timestamp"))
        timing_projection = self._trajectory_timing(record, timestamp)
        timing = timing_projection.record
        record_id = _trajectory_id(record.get("uuid") or record.get("id"))
        turn_id = _trajectory_id(
            record.get("turn_id") or record.get("turnId") or record.get("promptId")
        )
        if record.get("type") == "system" and record.get("subtype") == "turn_duration":
            turn_id = turn_id or timing_projection.turn_id
        step_id = _trajectory_id(record.get("step_id") or record.get("stepId"))
        facts: list[TrajectoryFact] = []

        def add(
            kind: TrajectoryKind,
            lane: TrajectoryLane,
            summary: str = "",
            *,
            native_id: str | None = None,
            status: TrajectoryStatus = TrajectoryStatus.UNKNOWN,
            turn: str | None = turn_id,
            step: str | None = step_id,
            request: str | None = None,
            call_id: str | None = None,
            parent_call_id: str | None = None,
            mcp_server: str | None = None,
            mcp_tool: str | None = None,
            fact_timing: Timing | None = timing,
            usage: TrajectoryUsage | None = None,
            failure: TrajectoryFailure | None = None,
            details: tuple[DetailField, ...] = (),
        ) -> None:
            clean_id = _trajectory_id(native_id)
            facts.append(
                TrajectoryFact(
                    kind=kind,
                    lane=lane,
                    source="claude",
                    summary=_safe_trajectory_text(summary),
                    status=status,
                    native_id=clean_id,
                    revision=_claude_revision(record),
                    raw_index=index,
                    event_ordinal=len(facts),
                    turn_id=turn,
                    step_id=step,
                    request_id=_trajectory_id(request),
                    call_id=_trajectory_id(call_id),
                    parent_call_id=_trajectory_id(parent_call_id),
                    mcp_server=_trajectory_id(mcp_server),
                    mcp_tool=_trajectory_id(mcp_tool),
                    timing=fact_timing,
                    usage=usage,
                    failure=failure,
                    details=details,
                )
            )

        kind = record.get("type")
        if kind == "assistant":
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            message_id = _trajectory_id(message.get("id"))
            message_turn = (
                _trajectory_id(message.get("turn_id") or message.get("turnId"))
                or message_id
                or _trajectory_id(record.get("requestId"))
                or turn_id
            )
            stop = message.get("stop_reason")
            message_status = _trajectory_status(
                message.get("status") or record.get("status"), TrajectoryStatus.COMPLETED
            )
            usage = _claude_trajectory_usage(message, record)
            request = _claude_request_id(message, record)
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}]
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                native_id = _claude_block_native_id(block, message_id, record_id, block_index)
                parent_call_id = _trajectory_id(
                    block.get("parent_call_id") or block.get("parentCallId")
                )
                if block_type == "text":
                    raw = _safe_trajectory_text(block.get("text"))
                    add(
                        TrajectoryKind.ASSISTANT,
                        TrajectoryLane.MODEL,
                        raw,
                        native_id=native_id,
                        status=message_status,
                        turn=message_turn,
                        request=request,
                        fact_timing=timing_projection.request,
                    )
                elif block_type == "thinking":
                    raw = _safe_trajectory_text(block.get("thinking"))
                    if not isinstance(block.get("thinking"), str):
                        continue
                    add(
                        TrajectoryKind.REASONING,
                        TrajectoryLane.MODEL,
                        raw,
                        native_id=native_id,
                        status=_trajectory_status(block.get("status"), message_status),
                        turn=message_turn,
                        request=request,
                        fact_timing=timing_projection.request,
                        details=(_trajectory_detail("thinking", raw, format=ContentFormat.TEXT),),
                    )
                elif block_type in ("tool_use", "server_tool_use"):
                    name = _safe_trajectory_text(block.get("name"))
                    mcp_identity = _claude_mcp_identity(name)
                    mcp_server, mcp_tool = mcp_identity or (None, None)
                    call_id = _trajectory_id(block.get("id") or block.get("call_id"))
                    input_value = block.get("input")
                    block_details = (
                        (_trajectory_detail("input", input_value, format=ContentFormat.JSON),)
                        if input_value is not None
                        else ()
                    )
                    add(
                        TrajectoryKind.TOOL_CALL,
                        TrajectoryLane.TOOLS,
                        name or "tool call",
                        native_id=native_id,
                        status=_trajectory_status(block.get("status"), TrajectoryStatus.PENDING),
                        turn=message_turn,
                        request=request,
                        call_id=call_id,
                        parent_call_id=parent_call_id,
                        mcp_server=mcp_server,
                        mcp_tool=mcp_tool,
                        details=block_details,
                    )
                elif block_type == "tool_result":
                    raw = _claude_content_text(block.get("content"))
                    call_id = _trajectory_id(block.get("tool_use_id") or block.get("call_id"))
                    mcp_identity = self._mcp_calls.get(call_id) if call_id is not None else None
                    mcp_server, mcp_tool = mcp_identity or (None, None)
                    result_status = (
                        TrajectoryStatus.ERROR
                        if block.get("is_error") is True
                        else _trajectory_status(block.get("status"), TrajectoryStatus.COMPLETED)
                    )
                    result_details = (
                        (
                            _trajectory_detail(
                                "result",
                                block.get("content"),
                                format=(
                                    ContentFormat.TEXT
                                    if isinstance(block.get("content"), str)
                                    else ContentFormat.JSON
                                ),
                            ),
                        )
                        if block.get("content") is not None
                        else ()
                    )
                    add(
                        TrajectoryKind.TOOL_RESULT,
                        TrajectoryLane.TOOLS,
                        raw,
                        native_id=native_id,
                        status=result_status,
                        turn=message_turn,
                        call_id=call_id,
                        parent_call_id=parent_call_id or call_id,
                        mcp_server=mcp_server,
                        mcp_tool=mcp_tool,
                        failure=(
                            TrajectoryFailure(TrajectoryFailureCategory.TOOL, detail=raw)
                            if block.get("is_error") is True
                            else None
                        ),
                        details=result_details,
                    )
            if not facts and (usage is not None or stop is not None):
                add(
                    TrajectoryKind.ASSISTANT,
                    TrajectoryLane.MODEL,
                    native_id=message_id or record_id,
                    status=message_status,
                    turn=message_turn,
                    request=request,
                    fact_timing=timing_projection.request,
                )
            if usage is not None:
                usage_native_id = (
                    f"{record_id}:usage"
                    if record_id is not None
                    else f"{message_id}:usage"
                    if message_id is not None
                    else None
                )
                add(
                    TrajectoryKind.USAGE,
                    TrajectoryLane.MODEL,
                    native_id=usage_native_id,
                    status=TrajectoryStatus.COMPLETED,
                    turn=message_turn,
                    request=request,
                    fact_timing=timing_projection.request,
                    usage=usage,
                )
            return facts

        if kind == "user":
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            message_id = _trajectory_id(message.get("id"))
            content = message.get("content")
            blocks = content if isinstance(content, list) else []
            if isinstance(content, str):
                blocks = [{"type": "text", "text": content}]
            user_status = _trajectory_status(
                message.get("status") or record.get("status"), TrajectoryStatus.COMPLETED
            )
            for block_index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")
                native_id = _claude_block_native_id(block, message_id, record_id, block_index)
                if block_type == "text":
                    add(
                        TrajectoryKind.USER,
                        TrajectoryLane.INPUT,
                        _safe_trajectory_text(block.get("text")),
                        native_id=native_id,
                        status=_trajectory_status(block.get("status"), user_status),
                    )
                elif block_type == "tool_result":
                    raw = _claude_content_text(block.get("content"))
                    call_id = _trajectory_id(block.get("tool_use_id") or block.get("call_id"))
                    mcp_identity = self._mcp_calls.get(call_id) if call_id is not None else None
                    mcp_server, mcp_tool = mcp_identity or (None, None)
                    parent_call_id = _trajectory_id(
                        block.get("parent_call_id") or block.get("parentCallId")
                    )
                    result_status = (
                        TrajectoryStatus.ERROR
                        if block.get("is_error") is True
                        else _trajectory_status(block.get("status"), TrajectoryStatus.COMPLETED)
                    )
                    details = (
                        (
                            _trajectory_detail(
                                "result",
                                block.get("content"),
                                format=(
                                    ContentFormat.TEXT
                                    if isinstance(block.get("content"), str)
                                    else ContentFormat.JSON
                                ),
                            ),
                        )
                        if block.get("content") is not None
                        else ()
                    )
                    add(
                        TrajectoryKind.TOOL_RESULT,
                        TrajectoryLane.TOOLS,
                        raw,
                        native_id=native_id,
                        status=result_status,
                        call_id=call_id,
                        parent_call_id=parent_call_id or call_id,
                        mcp_server=mcp_server,
                        mcp_tool=mcp_tool,
                        failure=(
                            TrajectoryFailure(TrajectoryFailureCategory.TOOL, detail=raw)
                            if block.get("is_error") is True
                            else None
                        ),
                        details=details,
                    )
            if not facts and (isinstance(content, str) or record_id is not None):
                add(
                    TrajectoryKind.USER,
                    TrajectoryLane.INPUT,
                    _safe_trajectory_text(content),
                    native_id=message_id or record_id,
                    status=user_status,
                )
            return facts

        if kind in ("system", "context", "summary"):
            error = record.get("error")
            subtype = _safe_trajectory_text(record.get("subtype") or record.get("type"))
            is_error = record.get("level") == "error"
            fact_kind = (
                TrajectoryKind.ERROR
                if is_error
                else TrajectoryKind.CONTEXT
                if kind in ("context", "summary")
                or any(token in subtype for token in ("context", "compact"))
                else TrajectoryKind.SYSTEM
            )
            body = error if is_error else record.get("content")
            if body is None:
                body = record.get("summary") or record.get("message") or subtype
            summary = _claude_content_text(body)
            system_details: list[DetailField] = []
            if error is not None:
                system_details.append(_trajectory_detail("error", error, format=ContentFormat.TEXT))
            if record.get("content") is not None and not isinstance(record.get("content"), str):
                system_details.append(
                    _trajectory_detail("content", record.get("content"), format=ContentFormat.JSON)
                )
            if record.get("durationMs") is not None:
                system_details.append(
                    _trajectory_detail(
                        "duration_ms", record.get("durationMs"), format=ContentFormat.JSON
                    )
                )
            metadata = {
                key: value
                for key, value in record.items()
                if key not in {"content", "message", "error", "timestamp", "type"}
            }
            if metadata:
                system_details.append(
                    _trajectory_detail("metadata", metadata, format=ContentFormat.JSON)
                )
            add(
                fact_kind,
                TrajectoryLane.THEATER
                if fact_kind is TrajectoryKind.ERROR
                else TrajectoryLane.MODEL,
                summary,
                native_id=record_id,
                status=(
                    TrajectoryStatus.ERROR
                    if is_error
                    else _trajectory_status(record.get("status"), TrajectoryStatus.COMPLETED)
                ),
                failure=(
                    TrajectoryFailure(TrajectoryFailureCategory.PROVIDER, detail=summary)
                    if is_error
                    else None
                ),
                details=tuple(system_details),
            )
        return facts

    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Sidechain records, deduplicated by the uuid that roots each one."""
        seen: set[str] = set()
        out: list[NativeChild] = []
        try:
            with transcript.open(encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    line = raw.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict) or not record.get("isSidechain"):
                        continue
                    root = record.get("parentUuid") or record.get("uuid")
                    if not root or root in seen:
                        continue
                    seen.add(root)
                    out.append(NativeChild(session_id=root, agent="task"))
        except OSError:
            return []
        return out
