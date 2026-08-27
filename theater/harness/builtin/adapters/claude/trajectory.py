"""Claude trajectory fact projection and bounded state."""

from __future__ import annotations

import json
from pathlib import Path
from typing import BinaryIO

from theater.constants.trajectory import (
    TRAJECTORY_MCP_CALL_CONTEXT_LIMIT,
    TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES,
)
from theater.harness.base import NativeChild
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.normalization.facts import fact_builder, tool_failure
from theater.harness.normalization.timing import iso_epoch as _epoch
from theater.harness.normalization.values import (
    content_blocks_text as _claude_content_text,
)
from theater.harness.normalization.values import (
    safe_trajectory_text as _safe_trajectory_text,
)
from theater.harness.normalization.values import (
    trajectory_detail as _trajectory_detail,
)
from theater.harness.normalization.values import (
    trajectory_identifier as _trajectory_id,
)
from theater.harness.normalization.values import (
    trajectory_status as _trajectory_status,
)
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryUsage

from .timing import (
    _claude_first_token,
    _claude_request_bounds,
    _claude_request_id,
    _claude_request_timing_value,
    _claude_timing,
    _claude_turn_timing,
    _ClaudeCausalRecord,
    _ClaudeRequestClock,
    _ClaudeTimingProjection,
)
from .usage import _claude_trajectory_usage
from .values import (
    _claude_block_native_id,
    _claude_mcp_identity,
    _claude_revision,
)

_claude_build = fact_builder(source="claude", identifier=_trajectory_id)


def _remember_bounded[ContextValue](
    mapping: dict[str, ContextValue], key: str, value: ContextValue
) -> None:
    mapping.pop(key, None)
    mapping[key] = value
    while len(mapping) > TRAJECTORY_MCP_CALL_CONTEXT_LIMIT:
        mapping.pop(next(iter(mapping)))


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
            facts.append(
                _claude_build(
                    kind=kind,
                    summary=_safe_trajectory_text(summary),
                    status=status,
                    lane_override=lane,
                    native_id=native_id,
                    revision=_claude_revision(record),
                    raw_index=index,
                    event_ordinal=len(facts),
                    turn_id=turn,
                    step_id=step,
                    request_id=request,
                    call_id=call_id,
                    parent_call_id=parent_call_id,
                    mcp_server=mcp_server,
                    mcp_tool=mcp_tool,
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
                        failure=tool_failure(
                            TrajectoryStatus.ERROR
                            if block.get("is_error") is True
                            else TrajectoryStatus.COMPLETED,
                            raw,
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
                        failure=tool_failure(
                            TrajectoryStatus.ERROR
                            if block.get("is_error") is True
                            else TrajectoryStatus.COMPLETED,
                            raw,
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
