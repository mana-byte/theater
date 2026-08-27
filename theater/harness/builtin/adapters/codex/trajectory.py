from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness.base import EventPath, NativeChild
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

from .constants import (
    _PATCH_FILE_RE,
    CODEX_MODEL_PROVIDER_ID_KEY,
    CODEX_MODEL_PROVIDER_KEY,
    CODEX_SESSION_META_RECORD_TYPE,
    CODEX_THREAD_SETTINGS_EVENT_TYPE,
)


def _event_path(value: str, *, cwd: str | None, mode: Literal["read", "write"]) -> EventPath | None:
    path = Path(value)
    if path.is_absolute():
        if cwd is None:
            return None
        try:
            path = path.resolve(strict=False).relative_to(Path(cwd).resolve(strict=False))
        except (OSError, ValueError):
            return None
    elif ".." in path.parts:
        return None
    rendered = path.as_posix()
    if rendered in {"", "."} or len(rendered) > 2048:
        return None
    return EventPath(path=rendered, mode=mode)


def _apply_patch_paths(text: str, *, cwd: str | None = None) -> tuple[EventPath, ...]:
    """Extract file paths from an ``apply_patch`` tool input string.

    The apply_patch format is a structured patch grammar with explicit
    per-file markers (``*** Update File:``, ``*** Add File:``, ``*** Delete
    File:``), not prose or a shell command. The markers and their grammar are
    defined in codex-rs/apply-patch/src/parser.rs:39-41. Every hunk is a write
    — update, create, and delete are all mutations — so every path gets
    ``mode="write"``.

    A malformed input yields nothing rather than a partial guess: a wrong
    path in the touch index is worse than a missing one.
    """
    if not isinstance(text, str):
        return ()
    paths = (
        _event_path(match.strip(), cwd=cwd, mode="write") for match in _PATCH_FILE_RE.findall(text)
    )
    return tuple(path for path in paths if path is not None)


def _patch_change_paths(value: object, *, cwd: str | None) -> tuple[EventPath, ...]:
    if not isinstance(value, Mapping):
        return ()
    paths: list[EventPath] = []
    for raw_path, change in value.items():
        if not isinstance(raw_path, str) or not isinstance(change, Mapping):
            continue
        if change.get("type") not in {"add", "delete", "update"}:
            continue
        candidates = [raw_path]
        move_path = change.get("move_path")
        if isinstance(move_path, str):
            candidates.append(move_path)
        paths.extend(
            path
            for candidate in candidates
            if (path := _event_path(candidate, cwd=cwd, mode="write")) is not None
        )
    return tuple(dict.fromkeys(paths))


def _epoch(value) -> float | None:
    """Codex writes ISO-8601 with a Z suffix, same as Claude Code."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _flatten(output) -> str:
    """Tool output is a list of `{"type": "input_text", "text": …}` blocks."""
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
    """The turn this record belongs to, as Codex names it.

    Stamped identically on `task_started` and on whichever record closes the
    turn, so the two ends of a turn are joinable without inference. Only read
    off the boundary records: the mid-turn `agent_message` and `user_message`
    events carry no turn_id at all, and inventing one for them by remembering
    the last `task_started` would mean holding state across lines, which
    parse() deliberately does not do.
    """
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
        """Codex has no sub-agent mechanism of its own."""
        return []
