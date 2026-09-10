"""Projection of Vibe Unified public history into Theater facts and events."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

from theater.harness.contracts.events import Event, EventKind, EventPath, clipper
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.normalization.facts import tool_failure
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import TimingProvenance, TrajectoryKind, TrajectoryStatus
from theater.trajectory.records import Timing

from .trajectory import (
    _relativise,
    _vibe_detail,
    _vibe_fact,
    _vibe_mcp_identity,
    _vibe_path_details,
    _vibe_text,
)

_TERMINAL_EFFECT_STATES = frozenset({"completed", "failed", "cancelled", "skipped"})
_WRITE_EFFECT_KINDS = frozenset({"file_edit", "file_write"})
_READ_EFFECT_KINDS = frozenset({"file_read", "file_search"})


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _entry_revision(watermark: int) -> int:
    """Use the store's monotone public-projection watermark as the revision."""
    return watermark


def _timestamp(value: object) -> float | None:
    milliseconds = _integer(value)
    return milliseconds / 1000 if milliseconds is not None else None


def _timing(entry: dict, *, duration_ms: object = None) -> Timing | None:
    start = _timestamp(entry.get("createdAt"))
    end = _timestamp(entry.get("updatedAt"))
    duration: float | None = None
    if isinstance(duration_ms, (int, float)) and not isinstance(duration_ms, bool):
        candidate = float(duration_ms)
        if math.isfinite(candidate) and candidate >= 0:
            duration = candidate
    if start is None and end is None and duration is None:
        return None
    return Timing(start=start, end=end, duration_ms=duration, provenance=TimingProvenance.SOURCE)


def _text_blocks(value: object) -> str:
    if not isinstance(value, list):
        return ""
    return "\n\n".join(
        text
        for block in value
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance((text := block.get("text")), str)
        and text
    )


def _status(value: object) -> TrajectoryStatus:
    if not isinstance(value, str):
        return TrajectoryStatus.UNKNOWN
    return {
        "pending": TrajectoryStatus.PENDING,
        "running": TrajectoryStatus.RUNNING,
        "blocked": TrajectoryStatus.RUNNING,
        "completed": TrajectoryStatus.COMPLETED,
        "failed": TrajectoryStatus.ERROR,
        "cancelled": TrajectoryStatus.CANCELLED,
        "skipped": TrajectoryStatus.CANCELLED,
    }.get(value, TrajectoryStatus.UNKNOWN)


def _entry_status(entry: dict) -> TrajectoryStatus:
    if entry.get("generationStatus") == "in_progress":
        return TrajectoryStatus.PARTIAL
    return TrajectoryStatus.COMPLETED


def _effect_paths(detail: dict, cwd: str | None) -> tuple[EventPath, ...]:
    kind = detail.get("kind")
    arguments = detail.get("input")
    if not isinstance(arguments, dict):
        return ()
    raw = next(
        (
            arguments.get(key)
            for key in ("filePath", "file_path", "path")
            if isinstance(arguments.get(key), str) and arguments.get(key)
        ),
        None,
    )
    if raw is None:
        return ()
    path = _relativise(raw, cwd)
    if path is None:
        return ()
    if kind in _WRITE_EFFECT_KINDS:
        return (EventPath(path=path, mode="write"),)
    if kind in _READ_EFFECT_KINDS:
        return (EventPath(path=path, mode="read"),)
    return ()


def _mcp_identity(tool_name: str | None, detail: dict) -> tuple[str, str] | None:
    if tool_name and tool_name.startswith("mcp_") and "." in tool_name:
        server, tool = tool_name.removeprefix("mcp_").split(".", 1)
        if server and tool:
            return server, tool
    return _vibe_mcp_identity(tool_name, presentation=detail)


def _decorate(parsed: ParsedRecord, *, revision: int, source_offset: int) -> ParsedRecord:
    return ParsedRecord(
        events=tuple(replace(event, source_offset=source_offset) for event in parsed.events),
        trajectory=tuple(
            replace(fact, revision=revision, source_offset=source_offset)
            for fact in parsed.trajectory
        ),
        trajectory_events=parsed.trajectory_events,
        status=parsed.status,
    )


def project_unified_entry(  # noqa: PLR0912, PLR0915
    entry: dict,
    *,
    identity: str,
    index: int,
    watermark: int,
    source_sequence: int,
    cwd: str | None,
    previous: dict | None = None,
    clip_text: bool = True,
) -> ParsedRecord:
    """Project one current public entry; ``previous`` controls one-shot bus events."""
    revision = _entry_revision(watermark)
    entry_type = entry.get("type")
    turn_id = _string(entry.get("turnId"))
    _clip = clipper(clip_text)

    if entry_type == "message":
        role = _string(entry.get("role"))
        text = _text_blocks(entry.get("content"))
        message_kinds = {
            "user": TrajectoryKind.USER,
            "assistant": TrajectoryKind.ASSISTANT,
            "system": TrajectoryKind.SYSTEM,
        }
        kind = (
            message_kinds.get(role, TrajectoryKind.UNKNOWN)
            if role is not None
            else TrajectoryKind.UNKNOWN
        )
        fact = _vibe_fact(
            kind=kind,
            summary=text,
            native_id=identity,
            revision=revision,
            raw_index=index,
            event_ordinal=0,
            status=_entry_status(entry),
            turn_id=turn_id,
            timing=_timing(entry),
        )
        completed_now = entry.get("generationStatus") == "completed" and (
            previous is None or previous.get("generationStatus") != "completed"
        )
        events: tuple[Event, ...] = ()
        if completed_now and role in {"user", "assistant"}:
            events = (
                Event(
                    kind=EventKind.USER if role == "user" else EventKind.ASSISTANT,
                    text=_clip(text),
                    raw_text=text,
                    ts=_timestamp(entry.get("updatedAt")),
                    turn_id=turn_id,
                    raw_index=index,
                ),
            )
        return _decorate(
            ParsedRecord(events=events, trajectory=(fact,), trajectory_events=()),
            revision=revision,
            source_offset=source_sequence,
        )

    if entry_type == "reasoning":
        text = _vibe_text(entry.get("text"))
        summaries = entry.get("summary")
        details: tuple[DetailField, ...] = ()
        if isinstance(summaries, list) and summaries:
            detail = _vibe_detail("summary", summaries, format=ContentFormat.JSON)
            details = (detail,) if detail is not None else ()
        fact = _vibe_fact(
            kind=TrajectoryKind.REASONING,
            summary=text,
            native_id=identity,
            revision=revision,
            raw_index=index,
            event_ordinal=0,
            status=_entry_status(entry),
            turn_id=turn_id,
            timing=_timing(entry),
            details=details,
        )
        return _decorate(
            ParsedRecord(trajectory=(fact,), trajectory_events=()),
            revision=revision,
            source_offset=source_sequence,
        )

    if entry_type == "effect":
        raw_detail = entry.get("detail")
        effect_detail: dict = raw_detail if isinstance(raw_detail, dict) else {}
        raw_state = entry.get("state")
        state: dict = raw_state if isinstance(raw_state, dict) else {}
        raw_prior_state = previous.get("state") if isinstance(previous, dict) else None
        prior_state: dict = raw_prior_state if isinstance(raw_prior_state, dict) else {}
        state_name = state.get("status")
        tool_name = _string(effect_detail.get("toolName"))
        effect_kind = _string(effect_detail.get("kind"))
        title = _string(entry.get("title")) or tool_name or "tool"
        paths = _effect_paths(effect_detail, cwd)
        mcp = _mcp_identity(tool_name, effect_detail)
        mcp_server, mcp_tool = mcp or (None, None)
        arguments = effect_detail.get("input")
        call_details = tuple(
            value
            for value in (
                _vibe_detail("arguments", arguments, format=ContentFormat.JSON),
                _vibe_detail("tool", tool_name),
                _vibe_detail("effect_kind", effect_kind),
                *_vibe_path_details(paths),
            )
            if value is not None
        )
        call = _vibe_fact(
            kind=TrajectoryKind.TOOL_CALL,
            summary=title,
            native_id=identity,
            revision=revision,
            raw_index=index,
            event_ordinal=0,
            status=_status(state_name),
            turn_id=turn_id,
            call_id=identity,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
            timing=_timing(entry),
            details=call_details,
        )
        output_text = state.get("outputText")
        output_text = output_text if isinstance(output_text, str) else ""
        output = state.get("output")
        error = state.get("error")
        if isinstance(error, dict):
            error_text = _string(error.get("message")) or _vibe_text(error)
        else:
            error_text = _vibe_text(error)
        terminal = state_name in _TERMINAL_EFFECT_STATES
        result_status = _status(state_name) if terminal else TrajectoryStatus.PARTIAL
        result_value = output if output is not None else output_text
        result_details = tuple(
            value
            for value in (
                _vibe_detail("result", result_value, format=ContentFormat.JSON),
                _vibe_detail("tool", tool_name),
            )
            if value is not None
        )
        facts: list[TrajectoryFact] = [call]
        if terminal or output_text or output is not None:
            facts.append(
                _vibe_fact(
                    kind=TrajectoryKind.TOOL_RESULT,
                    summary=error_text or output_text or title,
                    native_id=f"{identity}:result",
                    revision=revision,
                    raw_index=index,
                    event_ordinal=1,
                    status=result_status,
                    turn_id=turn_id,
                    call_id=identity,
                    mcp_server=mcp_server,
                    mcp_tool=mcp_tool,
                    timing=_timing(entry, duration_ms=state.get("durationMs")),
                    failure=tool_failure(result_status, error_text or "tool failed"),
                    details=result_details,
                )
            )
        effect_events: list[Event] = []
        if previous is None:
            effect_events.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=tool_name,
                    ts=_timestamp(entry.get("createdAt")),
                    turn_id=turn_id,
                    raw_index=index,
                    paths=paths,
                )
            )
        if terminal and prior_state.get("status") not in _TERMINAL_EFFECT_STATES:
            effect_events.append(
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(error_text or output_text),
                    raw_text=error_text or output_text,
                    tool_name=tool_name,
                    ts=_timestamp(entry.get("updatedAt")),
                    turn_id=turn_id,
                    raw_index=index,
                )
            )
        return _decorate(
            ParsedRecord(events=effect_events, trajectory=facts, trajectory_events=()),
            revision=revision,
            source_offset=source_sequence,
        )

    if entry_type in {"callback", "checkpoint", "notice"}:
        if entry_type == "checkpoint" and entry.get("kind") in {"compaction", "context_cleared"}:
            kind = TrajectoryKind.CONTEXT
        elif entry_type == "notice":
            kind = TrajectoryKind.SYSTEM
        else:
            kind = TrajectoryKind.UNKNOWN
        summary = next(
            (
                value
                for key in ("message", "title", "kind")
                if isinstance((value := entry.get(key)), str) and value
            ),
            entry_type,
        )
        detail = _vibe_detail("entry", entry, format=ContentFormat.JSON)
        fact = _vibe_fact(
            kind=kind,
            summary=summary,
            native_id=identity,
            revision=revision,
            raw_index=index,
            event_ordinal=0,
            status=_entry_status(entry),
            turn_id=turn_id,
            timing=_timing(entry),
            details=(detail,) if detail is not None else (),
        )
        return _decorate(
            ParsedRecord(trajectory=(fact,), trajectory_events=()),
            revision=revision,
            source_offset=source_sequence,
        )

    return ParsedRecord()


def entry_identity(entry: dict, occurrence: int) -> str | None:
    value = _string(entry.get("id"))
    if value is None:
        return None
    return value if occurrence == 1 else f"{value}#{occurrence}"


def entry_fingerprint(entry: dict) -> str:
    """Stable comparison representation; store integrity is checked by the reader."""
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def logical_stream_id(current: Path, session_id: str) -> str:
    """Stable logical identity without exposing the transcript path in resume floors."""
    import hashlib

    value = f"vibe-unified\0{current.resolve(strict=False)}\0{session_id}".encode()
    return f"vibe-unified:{hashlib.sha256(value).hexdigest()}"


__all__ = [
    "entry_fingerprint",
    "entry_identity",
    "logical_stream_id",
    "project_unified_entry",
]
