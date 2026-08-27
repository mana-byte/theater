"""Codex trajectory fact extraction."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from theater.harness.base import NativeChild
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryUsage

from .constants import (
    CODEX_SESSION_META_RECORD_TYPE,
    CODEX_THREAD_SETTINGS_EVENT_TYPE,
)
from .paths import _patch_change_paths
from .values import (
    _codex_block_id,
    _codex_content_text,
    _codex_mcp_identity,
    _codex_revision,
    _codex_scoped_id,
    _codex_timing,
    _codex_trajectory_turn_id,
    _codex_usage,
    _epoch,
    _safe_trajectory_text,
    _trajectory_detail,
    _trajectory_id,
    _trajectory_status,
    _turn_id,
)


class CodexTrajectoryMixin:
    if TYPE_CHECKING:
        _active_turn_id: str | None
        _last_cwd: str | None
        _last_model: str | None
        _last_provider: str | None
        _mcp_calls: dict[str, tuple[str, str]]
        _pending_patch_exec: tuple[str, float] | None

        def _mcp_result(self, result: object) -> str: ...

    def _trajectory_facts(  # noqa: PLR0912, PLR0915
        self, record: dict, index: int
    ) -> list[TrajectoryFact]:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return []
        timestamp = _epoch(record.get("timestamp"))
        timing = _codex_timing(record, payload, timestamp)
        record_id = _trajectory_id(record.get("id") or record.get("uuid"))
        turn_id = _codex_trajectory_turn_id(payload) or self._active_turn_id
        step_id = _trajectory_id(payload.get("step_id") or payload.get("stepId"))
        source_request_id = _trajectory_id(payload.get("request_id") or payload.get("requestId"))
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
            request_from_turn: bool = True,
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
                    source="codex",
                    summary=_safe_trajectory_text(summary),
                    status=status,
                    native_id=clean_id,
                    revision=_codex_revision(record, payload),
                    raw_index=index,
                    event_ordinal=len(facts),
                    turn_id=turn,
                    step_id=step,
                    request_id=_trajectory_id(
                        request if request is not None else source_request_id or turn
                    )
                    if request_from_turn
                    else None,
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

        record_kind = record.get("type")
        ptype = payload.get("type")
        if record_kind == CODEX_SESSION_META_RECORD_TYPE:
            session_id = _trajectory_id(payload.get("session_id") or payload.get("id"))
            add(
                TrajectoryKind.SYSTEM,
                TrajectoryLane.MODEL,
                "session metadata",
                native_id=session_id or record_id,
                status=_trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                request_from_turn=False,
                details=(_trajectory_detail("session", payload, format=ContentFormat.JSON),),
            )
            return facts

        if record_kind in ("turn_context", CODEX_THREAD_SETTINGS_EVENT_TYPE, "world_state"):
            context = payload.get("thread_context") or payload.get("thread_settings")
            context = context if context is not None else payload.get("state")
            if context is None:
                context = payload
            context_id = _trajectory_id(payload.get("id")) or record_id
            if context_id is None:
                context_id = _codex_scoped_id(turn_id, str(record_kind))
            model = None
            if isinstance(context, dict):
                model = _trajectory_id(context.get("model") or context.get("model_name"))
            summary = (
                "turn context"
                if record_kind == "turn_context"
                else str(record_kind).replace("_", " ")
            )
            if model:
                summary = f"{summary}: {model}"
            add(
                TrajectoryKind.CONTEXT,
                TrajectoryLane.MODEL,
                summary,
                native_id=context_id,
                turn=_codex_trajectory_turn_id(context) if isinstance(context, dict) else turn_id,
                status=_trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                details=(_trajectory_detail("context", context, format=ContentFormat.JSON),),
            )
            return facts

        if record_kind == "event_msg":
            event_type = payload.get("type")
            event_id = _trajectory_id(
                payload.get("id") or payload.get("message_id") or payload.get("item_id")
            )
            if event_type == "user_message":
                return facts
            if event_type == "agent_message":
                return facts
            if event_type == "task_complete":
                completed_timing = _codex_timing(record, payload, timestamp)
                add(
                    TrajectoryKind.CONTEXT,
                    TrajectoryLane.MODEL,
                    "turn completed",
                    native_id=_trajectory_id(payload.get("id"))
                    or _codex_scoped_id(turn_id, "completed"),
                    status=TrajectoryStatus.COMPLETED,
                    turn=_turn_id(payload),
                    fact_timing=completed_timing,
                    details=(
                        _trajectory_detail(
                            "output",
                            payload.get("last_agent_message"),
                            format=ContentFormat.TEXT,
                        ),
                    ),
                )
                return facts
            if event_type == "turn_aborted":
                reason = payload.get("reason") or "unknown"
                add(
                    TrajectoryKind.ERROR,
                    TrajectoryLane.THEATER,
                    f"turn aborted: {_safe_trajectory_text(reason)}",
                    native_id=event_id or _codex_scoped_id(turn_id, "aborted"),
                    status=TrajectoryStatus.INTERRUPTED,
                    turn=_turn_id(payload),
                )
                return facts
            if event_type == "patch_apply_end":
                call_id = _trajectory_id(payload.get("call_id"))
                paths = _patch_change_paths(payload.get("changes"), cwd=self._last_cwd)
                status_name = payload.get("status")
                status = (
                    TrajectoryStatus.CANCELLED
                    if status_name == "declined"
                    else TrajectoryStatus.COMPLETED
                    if payload.get("success") is True or status_name == "completed"
                    else TrajectoryStatus.ERROR
                )
                path_details = tuple(
                    _trajectory_detail(f"path.{path.mode}", path.path, format=ContentFormat.PATH)
                    for path in paths
                )
                call_timing = result_timing = timing
                if self._pending_patch_exec is not None and timestamp is not None:
                    started_at = self._pending_patch_exec[1]
                    if started_at < timestamp:
                        call_timing = Timing(
                            start=started_at,
                            provenance=TimingProvenance.DERIVED,
                        )
                        result_timing = Timing(
                            end=timestamp,
                            provenance=TimingProvenance.DERIVED,
                        )
                add(
                    TrajectoryKind.TOOL_CALL,
                    TrajectoryLane.TOOLS,
                    "apply_patch",
                    native_id=_codex_scoped_id(call_id, "call"),
                    status=status,
                    call_id=call_id,
                    fact_timing=call_timing,
                    details=(
                        _trajectory_detail(
                            "input",
                            {"files": [path.path for path in paths]},
                            format=ContentFormat.JSON,
                        ),
                        *path_details,
                    ),
                )
                patch_result = {
                    "success": payload.get("success") is True,
                    "status": status_name,
                    "stdout": payload.get("stdout") or "",
                    "stderr": payload.get("stderr") or "",
                }
                failure_detail = str(patch_result["stderr"] or patch_result["stdout"])
                add(
                    TrajectoryKind.TOOL_RESULT,
                    TrajectoryLane.TOOLS,
                    "patch applied" if status is TrajectoryStatus.COMPLETED else "patch failed",
                    native_id=_codex_scoped_id(call_id, "result"),
                    status=status,
                    call_id=call_id,
                    parent_call_id=call_id,
                    fact_timing=result_timing,
                    failure=(
                        TrajectoryFailure(
                            TrajectoryFailureCategory.TOOL,
                            code=str(status_name or "failed"),
                            detail=failure_detail,
                        )
                        if status is TrajectoryStatus.ERROR
                        else None
                    ),
                    details=(
                        _trajectory_detail("result", patch_result, format=ContentFormat.JSON),
                    ),
                )
                return facts
            if event_type in ("mcp_tool_call_begin", "mcp_tool_call_end"):
                invocation = payload.get("invocation")
                invocation = invocation if isinstance(invocation, dict) else {}
                mcp_parts = [invocation.get("server"), invocation.get("tool")]
                tool_name = ".".join(str(part) for part in mcp_parts if part)
                call_id = _trajectory_id(payload.get("call_id"))
                mcp_identity = _codex_mcp_identity(invocation)
                if mcp_identity is None and call_id is not None:
                    mcp_identity = self._mcp_calls.get(call_id)
                mcp_server, mcp_tool = mcp_identity or (None, None)
                if event_type == "mcp_tool_call_begin":
                    args = invocation.get("arguments") or invocation.get("input")
                    mcp_details = (
                        (_trajectory_detail("input", args, format=ContentFormat.JSON),)
                        if args is not None
                        else ()
                    )
                    add(
                        TrajectoryKind.TOOL_CALL,
                        TrajectoryLane.TOOLS,
                        tool_name or "MCP tool call",
                        native_id=event_id or _codex_scoped_id(call_id, "call"),
                        status=_trajectory_status(payload.get("status"), TrajectoryStatus.PENDING),
                        call_id=call_id,
                        parent_call_id=_trajectory_id(
                            payload.get("parent_call_id") or payload.get("parent_id")
                        ),
                        mcp_server=mcp_server,
                        mcp_tool=mcp_tool,
                        details=mcp_details,
                    )
                else:
                    result = payload.get("result")
                    raw = self._mcp_result(result)
                    result_error = isinstance(result, dict) and result.get("Err") is not None
                    if isinstance(result, dict):
                        ok = result.get("Ok")
                        if isinstance(ok, dict):
                            result_error = result_error or ok.get("isError") is True
                    add(
                        TrajectoryKind.TOOL_RESULT,
                        TrajectoryLane.TOOLS,
                        raw,
                        native_id=event_id or _codex_scoped_id(call_id, "result"),
                        status=TrajectoryStatus.ERROR
                        if result_error
                        else _trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                        call_id=call_id,
                        parent_call_id=_trajectory_id(
                            payload.get("parent_call_id") or payload.get("parent_id")
                        )
                        or call_id,
                        mcp_server=mcp_server,
                        mcp_tool=mcp_tool,
                        failure=(
                            TrajectoryFailure(TrajectoryFailureCategory.TOOL, detail=raw)
                            if result_error
                            else None
                        ),
                        details=(
                            (_trajectory_detail("result", result, format=ContentFormat.JSON),)
                            if result is not None
                            else ()
                        ),
                    )
                return facts
            if event_type == "token_count":
                usage = _codex_usage(
                    record,
                    payload,
                    model=self._last_model,
                    provider=self._last_provider,
                    request_id=turn_id,
                )
                if usage is not None:
                    add(
                        TrajectoryKind.USAGE,
                        TrajectoryLane.MODEL,
                        native_id=event_id,
                        status=TrajectoryStatus.COMPLETED,
                        usage=usage,
                    )
                return facts
            if event_type in (
                "task_started",
                "context_compacted",
                "turn_context",
                CODEX_THREAD_SETTINGS_EVENT_TYPE,
            ):
                status = (
                    TrajectoryStatus.RUNNING
                    if event_type == "task_started"
                    else _trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED)
                )
                add(
                    TrajectoryKind.CONTEXT,
                    TrajectoryLane.MODEL,
                    event_type.replace("_", " "),
                    native_id=event_id or _codex_scoped_id(turn_id, event_type),
                    status=status,
                    turn=_turn_id(payload),
                    details=(
                        (_trajectory_detail("payload", payload, format=ContentFormat.JSON),)
                        if payload
                        else ()
                    ),
                )
                return facts
            return facts

        if record_kind == "response_item":
            item_type = payload.get("type")
            item_id = _trajectory_id(payload.get("id"))
            item_turn = _codex_trajectory_turn_id(payload) or turn_id
            if item_type == "message":
                role = payload.get("role")
                content = payload.get("content")
                blocks = content if isinstance(content, list) else []
                if isinstance(content, str):
                    blocks = [{"type": "text", "text": content}]
                message_kind = (
                    TrajectoryKind.USER
                    if role == "user"
                    else TrajectoryKind.SYSTEM
                    if role in {"system", "developer"}
                    else TrajectoryKind.ASSISTANT
                )
                lane = (
                    TrajectoryLane.INPUT
                    if message_kind is TrajectoryKind.USER
                    else TrajectoryLane.MODEL
                )
                message_status = _trajectory_status(
                    payload.get("status"), TrajectoryStatus.COMPLETED
                )
                for block_index, block in enumerate(blocks):
                    if not isinstance(block, dict):
                        continue
                    block_text = block.get("text")
                    if not isinstance(block_text, str):
                        continue
                    add(
                        message_kind,
                        lane,
                        block_text,
                        native_id=_codex_block_id(item_id, block, block_index),
                        status=message_status,
                        turn=item_turn,
                        usage=_codex_usage(
                            record,
                            payload,
                            model=self._last_model,
                            provider=self._last_provider,
                            request_id=item_turn,
                        ),
                    )
                if not facts:
                    add(
                        message_kind,
                        lane,
                        _codex_content_text(content),
                        native_id=item_id,
                        status=message_status,
                        turn=item_turn,
                        usage=_codex_usage(
                            record,
                            payload,
                            model=self._last_model,
                            provider=self._last_provider,
                            request_id=item_turn,
                        ),
                    )
                return facts
            if item_type == "reasoning":
                reasoning_parts: list[tuple[str, dict]] = []
                reasoning_summary = payload.get("summary")
                if isinstance(reasoning_summary, str):
                    reasoning_parts.append(
                        (reasoning_summary, {"type": "summary_text", "text": reasoning_summary})
                    )
                elif isinstance(reasoning_summary, list):
                    for block in reasoning_summary:
                        if not isinstance(block, dict):
                            continue
                        block_text = block.get("text")
                        if isinstance(block_text, str):
                            reasoning_parts.append((block_text, block))
                content = payload.get("content")
                if isinstance(content, str):
                    reasoning_parts.append((content, {"type": "content", "text": content}))
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_text = block.get("text")
                        if isinstance(block_text, str):
                            reasoning_parts.append((block_text, block))
                for part_index, (text, block) in enumerate(reasoning_parts):
                    add(
                        TrajectoryKind.REASONING,
                        TrajectoryLane.MODEL,
                        _safe_trajectory_text(text),
                        status=_trajectory_status(
                            payload.get("status"), TrajectoryStatus.COMPLETED
                        ),
                        native_id=_codex_block_id(item_id, block, part_index),
                        turn=item_turn,
                        details=(
                            _trajectory_detail("reasoning", block, format=ContentFormat.JSON),
                        ),
                    )
                return facts
            call_id = _trajectory_id(payload.get("call_id"))
            parent_call_id = _trajectory_id(
                payload.get("parent_call_id") or payload.get("parent_id")
            )
            call_types = {
                "custom_tool_call",
                "function_call",
                "local_shell_call",
                "web_search_call",
                "computer_call",
                "mcp_tool_call",
            }
            result_types = {
                "custom_tool_call_output",
                "function_call_output",
                "local_shell_call_output",
                "web_search_call_output",
                "computer_call_output",
                "mcp_tool_call_output",
            }
            if item_type in call_types:
                name = _safe_trajectory_text(payload.get("name") or item_type)
                mcp_identity = (
                    _codex_mcp_identity(payload) if item_type == "mcp_tool_call" else None
                )
                mcp_server, mcp_tool = mcp_identity or (None, None)
                input_value = payload.get("input")
                if input_value is None:
                    input_value = payload.get("arguments")
                add(
                    TrajectoryKind.TOOL_CALL,
                    TrajectoryLane.TOOLS,
                    name,
                    native_id=item_id or call_id,
                    status=_trajectory_status(payload.get("status"), TrajectoryStatus.PENDING),
                    turn=item_turn,
                    call_id=call_id,
                    parent_call_id=parent_call_id,
                    mcp_server=mcp_server,
                    mcp_tool=mcp_tool,
                    usage=_codex_usage(
                        record,
                        payload,
                        model=self._last_model,
                        provider=self._last_provider,
                        request_id=item_turn,
                    ),
                    details=(
                        (_trajectory_detail("input", input_value, format=ContentFormat.JSON),)
                        if input_value is not None
                        else ()
                    ),
                )
            elif item_type in result_types:
                mcp_identity = _codex_mcp_identity(payload)
                if mcp_identity is None and item_type == "mcp_tool_call_output" and call_id:
                    mcp_identity = self._mcp_calls.get(call_id)
                mcp_server, mcp_tool = mcp_identity or (None, None)
                output = payload.get("output")
                if output is None:
                    output = payload.get("result")
                output_text = _codex_content_text(output)
                add(
                    TrajectoryKind.TOOL_RESULT,
                    TrajectoryLane.TOOLS,
                    output_text,
                    native_id=item_id,
                    status=_trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                    turn=item_turn,
                    call_id=call_id,
                    parent_call_id=parent_call_id or call_id,
                    mcp_server=mcp_server,
                    mcp_tool=mcp_tool,
                    details=(
                        (
                            _trajectory_detail(
                                "result",
                                output,
                                format=(
                                    ContentFormat.TEXT
                                    if isinstance(output, str)
                                    else ContentFormat.JSON
                                ),
                            ),
                        )
                        if output is not None
                        else ()
                    ),
                )
            return facts

        if record_kind in ("system", "context", "compaction") or ptype in (
            "system",
            "context",
            "compaction",
        ):
            body = payload.get("message") or payload.get("content") or payload.get("summary")
            system_details = (
                (_trajectory_detail("payload", payload, format=ContentFormat.JSON),)
                if payload
                else ()
            )
            add(
                TrajectoryKind.CONTEXT if ptype != "system" else TrajectoryKind.SYSTEM,
                TrajectoryLane.MODEL,
                _codex_content_text(body) or str(record_kind or ptype).replace("_", " "),
                native_id=record_id or _trajectory_id(payload.get("id")),
                status=_trajectory_status(payload.get("status"), TrajectoryStatus.COMPLETED),
                details=system_details,
            )
        return facts

    def native_children(self, transcript: Path) -> list[NativeChild]:
        return []
