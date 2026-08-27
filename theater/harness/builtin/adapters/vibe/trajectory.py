"""Vibe trajectory projection and record helpers."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness.base import SERVER_NAME, Event, EventPath, NativeChild
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

from .constants import _READ_TOOLS, _WRITE_TOOLS


def _extract_paths(
    tool_name: str | None, arguments: object, cwd: str | None
) -> tuple[EventPath, ...]:
    """Extract declared tool paths without leaking non-repository absolute paths."""
    if not tool_name or not arguments:
        return ()
    key = _WRITE_TOOLS.get(tool_name) or _READ_TOOLS.get(tool_name)
    if key is None:
        return ()
    mode: Literal["read", "write"] = "write" if tool_name in _WRITE_TOOLS else "read"
    if isinstance(arguments, dict):
        parsed = arguments
    elif isinstance(arguments, (str, bytes, bytearray)):
        try:
            parsed = json.loads(arguments)
        except (ValueError, TypeError):
            return ()
    else:
        return ()
    if not isinstance(parsed, dict):
        return ()
    raw = parsed.get(key)
    if not isinstance(raw, str) or not raw:
        return ()
    rel = _relativise(raw, cwd)
    if rel is None:
        return ()
    return (EventPath(path=rel, mode=mode),)


def _vibe_identifier(value) -> str | None:
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
    return f"vibe:{hashlib.sha256(encoded).hexdigest()}"


def _vibe_text(value) -> str:
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


def _vibe_detail(
    name: str, value, *, format: ContentFormat = ContentFormat.TEXT
) -> DetailField | None:
    if value is None:
        return None
    text = _vibe_text(value)
    if not text and isinstance(value, (int, float, bool)):
        text = json.dumps(value)
    if not text:
        return None
    try:
        return DetailField.from_text(name, text, format=format)
    except ValueError:
        return None


def _vibe_mcp_identity(value: object) -> tuple[str, str] | None:
    prefix = f"{SERVER_NAME}_"
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    tool = _vibe_identifier(value.removeprefix(prefix))
    return (SERVER_NAME, tool) if tool is not None else None


def _vibe_message_id(record: dict) -> str | None:
    return next(
        (
            value
            for key in ("message_id", "messageId", "id", "uuid")
            if isinstance((value := record.get(key)), str) and value
        ),
        None,
    )


def _vibe_duration(result: object) -> Timing | None:
    if not isinstance(result, dict):
        return None
    duration_ms = result.get("duration_ms")
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
        duration = result.get("duration")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            return None
        duration_ms = float(duration) * 1000
    value = float(duration_ms)
    if not math.isfinite(value) or value < 0:
        return None
    return Timing(duration_ms=value, provenance=TimingProvenance.SOURCE)


def _vibe_tagged_text(content: str, tag: str) -> str | None:
    stripped = content.strip()
    prefix = f"<{tag}>"
    suffix = f"</{tag}>"
    if not stripped.startswith(prefix):
        return None
    body = stripped[len(prefix) :]
    if body.endswith(suffix):
        body = body[: -len(suffix)]
    return body.strip()


def _vibe_presentation(value: object) -> tuple[str | None, str | None, bool | None]:
    if not isinstance(value, dict):
        return None, None, None
    kind = value.get("kind")
    kind = kind if isinstance(kind, str) and kind else None
    display = value.get("display")
    if not isinstance(display, dict):
        return kind, None, None
    message = next(
        (
            display.get(key)
            for key in ("message", "settledMessage", "summary", "statusText")
            if isinstance(display.get(key), str) and display.get(key)
        ),
        None,
    )
    success = display.get("success")
    return kind, message, success if isinstance(success, bool) else None


def _vibe_path_details(paths: tuple[EventPath, ...]) -> tuple[DetailField, ...]:
    return tuple(
        DetailField.from_text(f"path.{path.mode}", path.path, format=ContentFormat.PATH)
        for path in paths
    )


def _vibe_fact(
    *,
    kind: TrajectoryKind,
    summary: str,
    native_id: str | None,
    raw_index: int,
    event_ordinal: int,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    turn_id: str | None = None,
    call_id: str | None = None,
    parent_call_id: str | None = None,
    mcp_server: str | None = None,
    mcp_tool: str | None = None,
    request_id: str | None = None,
    timing: Timing | None = None,
    usage: TrajectoryUsage | None = None,
    failure: TrajectoryFailure | None = None,
    details: tuple[DetailField, ...] = (),
) -> TrajectoryFact:
    lane = (
        TrajectoryLane.INPUT
        if kind is TrajectoryKind.USER
        else TrajectoryLane.TOOLS
        if kind in (TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT)
        else TrajectoryLane.MODEL
    )
    return TrajectoryFact(
        kind=kind,
        lane=lane,
        source="vibe",
        summary=summary,
        status=status,
        native_id=_vibe_identifier(native_id),
        raw_index=max(0, raw_index),
        event_ordinal=max(0, event_ordinal),
        turn_id=_vibe_identifier(turn_id),
        request_id=_vibe_identifier(request_id),
        call_id=_vibe_identifier(call_id),
        parent_call_id=_vibe_identifier(parent_call_id),
        mcp_server=_vibe_identifier(mcp_server),
        mcp_tool=_vibe_identifier(mcp_tool),
        timing=timing,
        usage=usage,
        failure=failure,
        details=details,
    )


def _relativise(path: str, cwd: str | None) -> str | None:
    """Return a repository-relative path, or None for external absolute paths."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        return path
    if cwd is None:
        return None
    base = Path(cwd)
    try:
        return str(p.relative_to(base))
    except ValueError:
        return None


def usage_fact(event: Event, turn_id: str | None) -> TrajectoryFact | None:
    usage = event.usage
    if usage is None or usage.idempotency_key is None:
        return None
    return _vibe_fact(
        kind=TrajectoryKind.USAGE,
        summary=usage.model or "model usage",
        native_id=usage.idempotency_key,
        raw_index=0,
        event_ordinal=0,
        turn_id=turn_id,
        usage=TrajectoryUsage(
            model=usage.model,
            provider=usage.provider,
            request_id=usage.idempotency_key,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens,
            cache_write_tokens=usage.cache_creation_input_tokens,
            cost_usd=usage.cost_usd,
            cost_provenance=usage.cost_provenance,
        ),
    )


class VibeTrajectoryMixin:
    _cwd: str | None

    if TYPE_CHECKING:

        def _meta(self, session_dir: Path) -> dict: ...

    def _facts_for_record(
        self, record: dict, index: int, *, turn_id: str | None
    ) -> list[TrajectoryFact]:
        role = record.get("role")
        if role == "user":
            fact = self._user_fact(record, index, turn_id)
            return [fact] if fact is not None else []
        if role == "assistant":
            return self._assistant_facts(record, index, turn_id)
        if role == "tool":
            return [self._tool_result_fact(record, index, turn_id)]
        return []

    @staticmethod
    def _user_fact(record: dict, index: int, turn_id: str | None) -> TrajectoryFact | None:
        content = _vibe_text(record.get("content"))
        message_id = _vibe_message_id(record)
        if not content and message_id is None:
            return None
        boundary = record.get("context_boundary")
        details = tuple(
            detail
            for detail in (
                _vibe_detail("context_boundary", boundary),
                _vibe_detail("injected", record.get("injected")),
            )
            if detail is not None
        )
        return _vibe_fact(
            kind=TrajectoryKind.CONTEXT if boundary else TrajectoryKind.USER,
            summary=content,
            native_id=message_id,
            raw_index=index,
            event_ordinal=0,
            turn_id=turn_id,
            details=details,
        )

    def _assistant_facts(
        self, record: dict, index: int, turn_id: str | None
    ) -> list[TrajectoryFact]:
        message_id = _vibe_message_id(record)
        content = _vibe_text(record.get("content"))
        calls = record.get("tool_calls") or []
        facts: list[TrajectoryFact] = []
        if content or message_id is not None or not calls:
            facts.append(
                _vibe_fact(
                    kind=TrajectoryKind.ASSISTANT,
                    summary=content,
                    native_id=message_id,
                    raw_index=index,
                    event_ordinal=len(facts),
                    turn_id=turn_id,
                )
            )
        reasoning = record.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            reasoning_id = record.get("reasoning_message_id")
            facts.append(
                _vibe_fact(
                    kind=TrajectoryKind.REASONING,
                    summary=reasoning,
                    native_id=reasoning_id if isinstance(reasoning_id, str) else None,
                    raw_index=index,
                    event_ordinal=len(facts),
                    turn_id=turn_id,
                )
            )
        if not isinstance(calls, list):
            return facts
        for call in calls:
            if isinstance(call, dict):
                facts.append(self._tool_call_fact(call, index, len(facts), turn_id))
        return facts

    def _tool_call_fact(
        self, call: dict, index: int, ordinal: int, turn_id: str | None
    ) -> TrajectoryFact:
        function = call.get("function")
        if not isinstance(function, dict):
            function = {}
        call_id = call.get("id") or call.get("call_id")
        call_id = call_id if isinstance(call_id, str) else None
        name = function.get("name")
        name = name if isinstance(name, str) else None
        arguments = function.get("arguments")
        paths = _extract_paths(name, arguments, self._cwd)
        presentation_kind, presentation_message, _success = _vibe_presentation(
            call.get("presentation")
        )
        mcp_identity = _vibe_mcp_identity(name)
        mcp_server, mcp_tool = mcp_identity or (None, None)
        details = tuple(
            value
            for value in (
                _vibe_detail("arguments", arguments, format=ContentFormat.JSON),
                _vibe_detail("tool", name),
                _vibe_detail("presentation", presentation_kind),
                *_vibe_path_details(paths),
            )
            if value is not None
        )
        parent = call.get("parent_call_id") or call.get("parentCallId")
        parent = parent if isinstance(parent, str) else None
        return _vibe_fact(
            kind=TrajectoryKind.TOOL_CALL,
            summary=presentation_message or name or "",
            native_id=call_id,
            raw_index=index,
            event_ordinal=ordinal,
            status=TrajectoryStatus.UNKNOWN,
            turn_id=turn_id,
            call_id=call_id,
            parent_call_id=parent,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
            details=details,
        )

    @staticmethod
    def _tool_result_fact(record: dict, index: int, turn_id: str | None) -> TrajectoryFact:
        content = _vibe_text(record.get("content"))
        result = record.get("tool_result")
        result_data = result if isinstance(result, dict) else {}
        call_id = record.get("tool_call_id") or record.get("call_id")
        call_id = call_id if isinstance(call_id, str) else None
        name = record.get("name")
        name = name if isinstance(name, str) else None
        mcp_identity = _vibe_mcp_identity(name)
        mcp_server, mcp_tool = mcp_identity or (None, None)
        presentation = result_data.get("presentation")
        presentation_kind, presentation_message, presentation_success = _vibe_presentation(
            presentation
        )
        projected = presentation.get("projectedOutput") if isinstance(presentation, dict) else None
        output = projected if projected is not None else result_data.get("output", result)
        error = _vibe_text(record.get("error")) or _vibe_tagged_text(content, "tool_error")
        cancelled = result_data.get("cancelled") is True or (
            _vibe_tagged_text(content, "user_cancellation") is not None
        )
        if cancelled:
            status = TrajectoryStatus.CANCELLED
        elif error is not None or presentation_success is False:
            status = TrajectoryStatus.ERROR
        else:
            status = TrajectoryStatus.COMPLETED
        failure_detail = error or (
            presentation_message if status is TrajectoryStatus.ERROR else None
        )
        details = tuple(
            value
            for value in (
                _vibe_detail("result", output if output is not None else content),
                _vibe_detail("tool", name),
                _vibe_detail("presentation", presentation_kind),
            )
            if value is not None
        )
        return _vibe_fact(
            kind=TrajectoryKind.TOOL_RESULT,
            summary=failure_detail or presentation_message or content or _vibe_text(output),
            native_id=f"{call_id}:result" if call_id else None,
            raw_index=index,
            event_ordinal=0,
            status=status,
            turn_id=turn_id,
            call_id=call_id,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
            timing=_vibe_duration(result),
            failure=(
                TrajectoryFailure(
                    TrajectoryFailureCategory.TOOL,
                    detail=failure_detail or "tool failed",
                )
                if status is TrajectoryStatus.ERROR
                else None
            ),
            details=details,
        )

    def native_children(self, transcript: Path) -> list[NativeChild]:
        """Read the session's own list of sub-agents from meta.json."""
        entries = self._meta(transcript.parent).get("child_sessions") or []
        out: list[NativeChild] = []
        for entry in entries:
            if not isinstance(entry, dict) or not entry.get("session_id"):
                continue
            out.append(
                NativeChild(
                    session_id=entry["session_id"],
                    agent=entry.get("agent"),
                    relative_path=entry.get("relative_path"),
                    tool_call_id=entry.get("tool_call_id"),
                )
            )
        return out
