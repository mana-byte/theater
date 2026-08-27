"""OpenCode stored and live fact projection."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from typing import TYPE_CHECKING

from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.normalization.facts import tool_failure
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import TrajectoryFailureCategory, TrajectoryKind, TrajectoryStatus
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryUsage

from .store import live_revision_row, message_coordinate
from .values import (
    _assistant_request_id,
    _finish_status,
    _message_timing,
    _opencode_mcp_identity,
    _part_timing,
    _stored_fact,
    _table,
    _tool_output,
    _tool_status,
    _trajectory_detail,
    _trajectory_identifier,
    _trajectory_lane,
    _trajectory_string,
    _trajectory_text,
    _trajectory_usage,
    load_json_object,
)


class OpenCodeTrajectory:
    _text: dict[str, dict[str, str]]
    _trajectory_revisions: dict[str, int]
    _trajectory_signatures: dict[str, TrajectoryFact]

    if TYPE_CHECKING:

        def _history_coordinate(self, created: object) -> int: ...

        def _history_revision(self, updated: object, created: object) -> int: ...

        def _role(self, conn: sqlite3.Connection, mid: str) -> str | None: ...

        def _stored_revision(self, data: dict, updated: object, created: object) -> int: ...

    def _stored_facts_for_message(
        self,
        info: dict,
        parts: Sequence[tuple[object, object, object, object]],
        *,
        raw_index: int,
        message_revision: int,
    ) -> list[TrajectoryFact]:
        facts: list[TrajectoryFact] = []
        mid = _trajectory_string(info.get("id"))
        role = info.get("role")
        finish = info.get("finish")
        timing = _message_timing(info)
        usage = _trajectory_usage(info)
        request_id = _assistant_request_id(usage, mid) if role == "assistant" else None
        ordinal = 0
        for part_id, created, updated, raw in parts:
            part = load_json_object(raw)
            if not isinstance(part.get("id"), str):
                part["id"] = str(part_id)
            part_facts = self._stored_facts_for_part(
                info,
                part,
                revision=self._stored_revision(part, updated, created),
                raw_index=raw_index,
                ordinal_base=ordinal,
                timing=timing,
                usage=usage,
                request_id=request_id,
            )
            facts.extend(part_facts)
            ordinal += max(1, len(part_facts))
        if not facts and role in ("user", "system", "developer"):
            content = _trajectory_text(info.get("content"))
            if content:
                facts.append(
                    _stored_fact(
                        kind=TrajectoryKind.USER if role == "user" else TrajectoryKind.SYSTEM,
                        summary=content,
                        status=TrajectoryStatus.COMPLETED,
                        native_id=mid or None,
                        fallback_id=None,
                        revision=message_revision,
                        raw_index=raw_index,
                        event_ordinal=0,
                        turn_id=mid or None,
                        timing=timing,
                    )
                )
        if not facts and role == "assistant" and (finish or usage is not None):
            facts.append(
                _stored_fact(
                    kind=TrajectoryKind.ASSISTANT,
                    summary="",
                    status=_finish_status(finish),
                    native_id=mid or None,
                    fallback_id=None,
                    revision=message_revision,
                    raw_index=raw_index,
                    event_ordinal=0,
                    turn_id=mid or None,
                    timing=timing,
                    usage=usage,
                    request_id=request_id,
                )
            )
        return facts

    def _stored_facts_for_part(
        self,
        info: dict,
        part: dict,
        *,
        revision: int,
        raw_index: int,
        ordinal_base: int,
        timing: Timing | None,
        usage: TrajectoryUsage | None,
        request_id: str | None,
    ) -> list[TrajectoryFact]:
        mid = _trajectory_string(info.get("id"))
        role = info.get("role")
        ptype = part.get("type")
        part_id = part.get("id") if isinstance(part.get("id"), str) else None
        fallback = part_id if isinstance(part_id, str) else None
        if ptype == "text":
            text = _trajectory_string(part.get("text"))
            if role == "assistant":
                kind = TrajectoryKind.ASSISTANT
                status = _finish_status(info.get("finish"))
                fact_usage = usage
                fact_timing = timing or _part_timing(part)
            elif role == "user":
                kind = TrajectoryKind.USER
                status = TrajectoryStatus.COMPLETED
                fact_usage = None
                fact_timing = timing or _part_timing(part)
            elif role in ("system", "developer"):
                kind = TrajectoryKind.SYSTEM
                status = TrajectoryStatus.COMPLETED
                fact_usage = None
                fact_timing = timing or _part_timing(part)
            else:
                return []
            return [
                _stored_fact(
                    kind=kind,
                    summary=text,
                    status=status,
                    native_id=part_id,
                    fallback_id=fallback,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base,
                    turn_id=mid or None,
                    timing=fact_timing,
                    usage=fact_usage,
                    request_id=request_id if role == "assistant" else None,
                )
            ]
        if ptype in ("reasoning", "thinking"):
            text = _trajectory_string(part.get("text"))
            part_timing = _part_timing(part)
            status = (
                TrajectoryStatus.COMPLETED
                if part_timing is not None and part_timing.end is not None
                else _finish_status(info.get("finish"))
            )
            return [
                _stored_fact(
                    kind=TrajectoryKind.REASONING,
                    summary=text,
                    status=status,
                    native_id=part_id,
                    fallback_id=fallback,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base,
                    turn_id=mid or None,
                    timing=part_timing,
                    request_id=request_id if role == "assistant" else None,
                )
            ]
        if ptype in ("context", "system"):
            text = _trajectory_text(part.get("text") or part.get("content"))
            return [
                _stored_fact(
                    kind=TrajectoryKind.SYSTEM if ptype == "system" else TrajectoryKind.CONTEXT,
                    summary=text,
                    status=TrajectoryStatus.COMPLETED,
                    native_id=part_id,
                    fallback_id=fallback,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base,
                    turn_id=mid or None,
                    timing=_part_timing(part),
                )
            ]
        if ptype != "tool":
            return []
        state = _table(part.get("state"))
        state_status = state.get("status")
        call = part.get("callID") or part.get("id")
        call_id = call if isinstance(call, str) else None
        tool_name = part.get("tool") if isinstance(part.get("tool"), str) else None
        mcp_identity = _opencode_mcp_identity(tool_name)
        mcp_server, mcp_tool = mcp_identity or (None, None)
        parent = part.get("parentCallID") or state.get("parentCallID")
        parent_id = parent if isinstance(parent, str) else None
        details = [
            value
            for value in (
                _trajectory_detail("tool", tool_name),
                _trajectory_detail("arguments", state.get("input"), format=ContentFormat.JSON),
            )
            if value is not None
        ]
        facts = [
            _stored_fact(
                kind=TrajectoryKind.TOOL_CALL,
                summary=tool_name or "",
                status=_tool_status(state_status),
                native_id=call_id,
                fallback_id=fallback,
                revision=revision,
                raw_index=raw_index,
                event_ordinal=ordinal_base,
                turn_id=mid or None,
                call_id=call_id,
                parent_call_id=parent_id,
                mcp_server=mcp_server,
                mcp_tool=mcp_tool,
                timing=_part_timing(part),
                request_id=request_id if role == "assistant" else None,
                details=details,
            )
        ]
        if state_status in ("completed", "error"):
            result = _tool_output(state)
            result_detail = _trajectory_detail("result", result)
            facts.append(
                _stored_fact(
                    kind=TrajectoryKind.TOOL_RESULT,
                    summary=result,
                    status=(
                        TrajectoryStatus.ERROR
                        if state_status == "error"
                        else TrajectoryStatus.COMPLETED
                    ),
                    native_id=f"{call_id}:result" if call_id else None,
                    fallback_id=f"{fallback}:result" if fallback else None,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base + 1,
                    turn_id=mid or None,
                    call_id=call_id,
                    parent_call_id=parent_id,
                    mcp_server=mcp_server,
                    mcp_tool=mcp_tool,
                    failure=tool_failure(
                        TrajectoryStatus.ERROR
                        if state_status == "error"
                        else TrajectoryStatus.COMPLETED,
                        result,
                    ),
                    timing=_part_timing(part),
                    request_id=request_id if role == "assistant" else None,
                    details=(result_detail,) if result_detail is not None else (),
                )
            )
        return facts

    def _live_fact(
        self,
        *,
        kind: TrajectoryKind,
        summary: str,
        status: TrajectoryStatus,
        native_id: str | None,
        fallback_id: str | None,
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
        revision_hint: int | None = None,
    ) -> TrajectoryFact | None:
        native = _trajectory_identifier(native_id, "native")
        if native is None:
            native = _trajectory_identifier(fallback_id, "fallback")
        candidate = TrajectoryFact(
            kind=kind,
            lane=_trajectory_lane(kind),
            source="opencode",
            summary=summary,
            status=status,
            native_id=native,
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
        key = native or f"fallback:{candidate.raw_index}:{candidate.event_ordinal}:{kind.value}"
        previous = self._trajectory_signatures.get(key)
        if previous is not None:
            comparable = replace(
                candidate,
                raw_index=previous.raw_index,
                event_ordinal=previous.event_ordinal,
            )
            if previous == comparable:
                return None
        revision = max(self._trajectory_revisions.get(key, -1) + 1, revision_hint or 0)
        self._trajectory_revisions[key] = revision
        self._trajectory_signatures[key] = candidate
        return replace(candidate, revision=revision)

    def _live_revision(
        self,
        conn: sqlite3.Connection,
        table: str,
        record_id: str | None,
        *fallbacks: object,
    ) -> int:
        values = list(fallbacks)
        if table not in {"message", "part"}:
            raise ValueError("unsupported OpenCode revision table")
        if record_id:
            row = live_revision_row(conn, table, record_id)
            if row is not None:
                values[:0] = row
        revisions = [self._history_revision(value, 0) for value in values]
        return max(revisions, default=0)

    def _message_coordinate(
        self, conn: sqlite3.Connection, message_id: object, fallback: int
    ) -> int:
        if isinstance(message_id, str) and message_id:
            row = message_coordinate(conn, message_id)
            if row is not None:
                return self._history_coordinate(row[0])
        return max(0, fallback)

    def _trajectory_for_part(  # noqa: PLR0912, PLR0915
        self, conn: sqlite3.Connection, payload: dict, seq: int, *, raw_index: int
    ) -> list[TrajectoryFact]:
        part = payload.get("part")
        if not isinstance(part, dict):
            return []
        mid = part.get("messageID")
        message_id = mid if isinstance(mid, str) else ""
        role = self._role(conn, message_id) if message_id else None
        request_id = _assistant_request_id(None, message_id) if role == "assistant" else None
        ptype = part.get("type")
        timing = _part_timing(part)
        part_id = part.get("id")
        fallback = part_id if isinstance(part_id, str) else None
        revision_hint = self._live_revision(
            conn,
            "part",
            fallback,
            payload.get("time"),
            seq,
        )
        step_id = part.get("stepID") or part.get("stepId")
        if ptype == "text":
            if role == "assistant":
                kind = TrajectoryKind.ASSISTANT
                status = TrajectoryStatus.RUNNING
            elif role == "user":
                kind = TrajectoryKind.USER
                status = TrajectoryStatus.COMPLETED
            elif role in ("system", "developer"):
                kind = TrajectoryKind.SYSTEM
                status = TrajectoryStatus.COMPLETED
            else:
                return []
            text = _trajectory_string(part.get("text"))
            fact = self._live_fact(
                kind=kind,
                summary=text,
                status=status,
                native_id=part_id,
                fallback_id=fallback,
                raw_index=raw_index,
                event_ordinal=0,
                turn_id=message_id or None,
                step_id=step_id if isinstance(step_id, str) else None,
                request_id=request_id,
                timing=timing,
                revision_hint=revision_hint,
            )
            return [fact] if fact is not None else []
        if ptype in ("reasoning", "thinking"):
            text = _trajectory_string(part.get("text"))
            status = (
                TrajectoryStatus.COMPLETED
                if timing is not None and timing.end is not None
                else TrajectoryStatus.RUNNING
            )
            fact = self._live_fact(
                kind=TrajectoryKind.REASONING,
                summary=text,
                status=status,
                native_id=part_id,
                fallback_id=fallback,
                raw_index=raw_index,
                event_ordinal=0,
                turn_id=message_id or None,
                step_id=step_id if isinstance(step_id, str) else None,
                request_id=request_id,
                timing=timing,
                revision_hint=revision_hint,
            )
            return [fact] if fact is not None else []
        if ptype in ("context", "system"):
            text = _trajectory_text(part.get("text") or part.get("content"))
            fact = self._live_fact(
                kind=TrajectoryKind.SYSTEM if ptype == "system" else TrajectoryKind.CONTEXT,
                summary=text,
                status=TrajectoryStatus.COMPLETED,
                native_id=part_id,
                fallback_id=fallback,
                raw_index=raw_index,
                event_ordinal=0,
                turn_id=message_id or None,
                step_id=step_id if isinstance(step_id, str) else None,
                timing=timing,
                revision_hint=revision_hint,
            )
            return [fact] if fact is not None else []
        if ptype != "tool":
            return []
        state = _table(part.get("state"))
        state_status = state.get("status")
        call = part.get("callID") or part.get("id")
        call_id = call if isinstance(call, str) else None
        tool_name = part.get("tool") if isinstance(part.get("tool"), str) else None
        mcp_identity = _opencode_mcp_identity(tool_name)
        mcp_server, mcp_tool = mcp_identity or (None, None)
        parent = part.get("parentCallID") or state.get("parentCallID")
        parent_id = parent if isinstance(parent, str) else None
        details: list[DetailField] = []
        tool_detail = _trajectory_detail("tool", tool_name)
        if tool_detail is not None:
            details.append(tool_detail)
        input_detail = _trajectory_detail(
            "arguments", state.get("input"), format=ContentFormat.JSON
        )
        if input_detail is not None:
            details.append(input_detail)
        call_fact = self._live_fact(
            kind=TrajectoryKind.TOOL_CALL,
            summary=tool_name or "",
            status=_tool_status(state_status),
            native_id=call_id,
            fallback_id=fallback,
            raw_index=raw_index,
            event_ordinal=0,
            turn_id=message_id or None,
            step_id=step_id if isinstance(step_id, str) else None,
            request_id=request_id,
            call_id=call_id,
            parent_call_id=parent_id,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
            timing=timing,
            details=details,
            revision_hint=revision_hint,
        )
        facts = [call_fact] if call_fact is not None else []
        if state_status in ("completed", "error"):
            result = _tool_output(state)
            result_details: list[DetailField] = []
            result_detail = _trajectory_detail("result", result)
            if result_detail is not None:
                result_details.append(result_detail)
            result_fact = self._live_fact(
                kind=TrajectoryKind.TOOL_RESULT,
                summary=result,
                status=(
                    TrajectoryStatus.ERROR
                    if state_status == "error"
                    else TrajectoryStatus.COMPLETED
                ),
                native_id=f"{call_id}:result" if call_id else None,
                fallback_id=f"{fallback}:result" if fallback else None,
                raw_index=raw_index,
                event_ordinal=1,
                turn_id=message_id or None,
                step_id=step_id if isinstance(step_id, str) else None,
                request_id=request_id,
                call_id=call_id,
                parent_call_id=parent_id,
                mcp_server=mcp_server,
                mcp_tool=mcp_tool,
                failure=(
                    TrajectoryFailure(TrajectoryFailureCategory.TOOL, detail=result)
                    if state_status == "error"
                    else None
                ),
                timing=timing,
                details=result_details,
                revision_hint=revision_hint,
            )
            if result_fact is not None:
                facts.append(result_fact)
        return facts

    def _trajectory_for_message(
        self, conn: sqlite3.Connection, payload: dict, seq: int, *, raw_index: int
    ) -> list[TrajectoryFact]:
        info = payload.get("info")
        if not isinstance(info, dict):
            return []
        mid = _trajectory_string(info.get("id"))
        role = info.get("role")
        finish = info.get("finish")
        timing = _message_timing(info)
        usage = _trajectory_usage(info)
        request_id = _assistant_request_id(usage, mid) if role == "assistant" else None
        time_data = _table(info.get("time"))
        revision_hint = self._live_revision(
            conn,
            "message",
            mid or None,
            time_data.get("updated"),
            time_data.get("completed"),
            time_data.get("created"),
            seq,
        )
        if role == "assistant":
            text_parts = self._text.get(mid, {})
            if text_parts:
                facts: list[TrajectoryFact] = []
                status = _finish_status(finish)
                for ordinal, (part_id, part_text) in enumerate(text_parts.items()):
                    fact = self._live_fact(
                        kind=TrajectoryKind.ASSISTANT,
                        summary=part_text,
                        status=status,
                        native_id=part_id,
                        fallback_id=f"{mid}:text" if mid else None,
                        raw_index=raw_index,
                        event_ordinal=ordinal,
                        turn_id=mid or None,
                        timing=timing,
                        usage=usage,
                        request_id=request_id,
                        revision_hint=revision_hint,
                    )
                    if fact is not None:
                        facts.append(fact)
                return facts
            if finish or usage is not None:
                fact = self._live_fact(
                    kind=TrajectoryKind.ASSISTANT,
                    summary="",
                    status=_finish_status(finish),
                    native_id=mid or None,
                    fallback_id=None,
                    raw_index=raw_index,
                    event_ordinal=0,
                    turn_id=mid or None,
                    timing=timing,
                    usage=usage,
                    request_id=request_id,
                    revision_hint=revision_hint,
                )
                return [fact] if fact is not None else []
            return []
        if role in ("user", "system", "developer"):
            content = _trajectory_text(info.get("content"))
            if not content:
                return []
            kind = TrajectoryKind.USER if role == "user" else TrajectoryKind.SYSTEM
            fact = self._live_fact(
                kind=kind,
                summary=content,
                status=TrajectoryStatus.COMPLETED,
                native_id=mid or None,
                fallback_id=None,
                raw_index=raw_index,
                event_ordinal=0,
                turn_id=mid or None,
                timing=timing,
                revision_hint=revision_hint,
            )
            return [fact] if fact is not None else []
        return []
