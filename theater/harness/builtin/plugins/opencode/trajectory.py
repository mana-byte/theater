"""OpenCode stored and live fact projection."""

from __future__ import annotations

import sqlite3
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.normalization.facts import tool_failure
from theater.harness.normalization.values import json_container_format
from theater.trajectory.content import ContentFormat, DetailField
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus
from theater.trajectory.records import Timing, TrajectoryFailure, TrajectoryUsage

from . import queries
from .constants import LIVE_TRAJECTORY_STATE_LIMIT
from .mcp import OpenCodeMcpCatalog
from .values import (
    _assistant_request_id,
    _finish_status,
    _has_tool_calls,
    _message_timing,
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


@dataclass(frozen=True)
class _ToolPart:
    name: str | None
    call_id: str | None
    parent_id: str | None
    fallback_id: str | None
    result_id: str | None
    result_fallback_id: str | None
    call_status: TrajectoryStatus
    call_details: tuple[DetailField, ...]
    result_status: TrajectoryStatus | None
    result_text: str | None
    result_details: tuple[DetailField, ...]
    result_failure: TrajectoryFailure | None
    timing: Timing | None


def _interpret_tool_part(part: dict) -> _ToolPart:
    """Read tool identity and content once; live and history decide their own update policy."""
    state = _table(part.get("state"))
    state_status = state.get("status")
    call = part.get("callID") or part.get("id")
    call_id = call if isinstance(call, str) else None
    fallback = part.get("id") if isinstance(part.get("id"), str) else None
    name = part.get("tool") if isinstance(part.get("tool"), str) else None
    parent = part.get("parentCallID") or state.get("parentCallID")
    parent_id = parent if isinstance(parent, str) else None
    call_details = tuple(
        value
        for value in (
            _trajectory_detail("tool", name),
            _trajectory_detail("arguments", state.get("input"), format=ContentFormat.JSON),
        )
        if value is not None
    )
    result_status = _tool_status(state_status) if state_status in ("completed", "error") else None
    result = _tool_output(state) if result_status is not None else None
    result_detail = (
        _trajectory_detail("result", result, format=json_container_format(result))
        if result is not None
        else None
    )
    return _ToolPart(
        name=name,
        call_id=call_id,
        parent_id=parent_id,
        fallback_id=fallback,
        result_id=f"{call_id}:result" if call_id else None,
        result_fallback_id=f"{fallback}:result" if fallback else None,
        call_status=_tool_status(state_status),
        call_details=call_details,
        result_status=result_status,
        result_text=result,
        result_details=(result_detail,) if result_detail is not None else (),
        result_failure=(
            tool_failure(result_status, result)
            if result_status is not None and result is not None
            else None
        ),
        timing=_part_timing(part),
    )


class OpenCodeTrajectory:
    _mcp_catalog: OpenCodeMcpCatalog
    _mcp_catalog_generation: int
    _text: dict[str, dict[str, str]]
    _trajectory_state: OrderedDict[str, tuple[int, TrajectoryFact]]

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
        has_tool_calls = _has_tool_calls(load_json_object(raw) for _, _, _, raw in parts)
        for index, (part_id, created, updated, raw) in enumerate(parts):
            part = load_json_object(raw)
            if not isinstance(part.get("id"), str):
                part["id"] = str(part_id)
            part_facts = self._stored_facts_for_part(
                info,
                part,
                revision=self._stored_revision(part, updated, created),
                raw_index=raw_index,
                # Reserve the result slot before a running tool completes.
                ordinal_base=index * 2,
                timing=timing,
                usage=usage,
                request_id=request_id,
                has_tool_calls=has_tool_calls,
            )
            facts.extend(part_facts)
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
                    status=_finish_status(finish, has_tool_calls),
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
        has_tool_calls: bool = False,
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
                status = _finish_status(info.get("finish"), has_tool_calls)
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
                else _finish_status(info.get("finish"), has_tool_calls)
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
        tool = _interpret_tool_part(part)
        mcp_identity = self._opencode_mcp_identity(tool.name)
        mcp_server, mcp_tool = mcp_identity or (None, None)
        facts = [
            _stored_fact(
                kind=TrajectoryKind.TOOL_CALL,
                summary=tool.name or "",
                status=tool.call_status,
                native_id=tool.call_id,
                fallback_id=tool.fallback_id,
                revision=revision,
                raw_index=raw_index,
                event_ordinal=ordinal_base,
                turn_id=mid or None,
                call_id=tool.call_id,
                parent_call_id=tool.parent_id,
                mcp_server=mcp_server,
                mcp_tool=mcp_tool,
                timing=tool.timing,
                request_id=request_id if role == "assistant" else None,
                details=tool.call_details,
            )
        ]
        if tool.result_status is not None and tool.result_text is not None:
            facts.append(
                _stored_fact(
                    kind=TrajectoryKind.TOOL_RESULT,
                    summary=tool.result_text,
                    status=tool.result_status,
                    native_id=tool.result_id,
                    fallback_id=tool.result_fallback_id,
                    revision=revision,
                    raw_index=raw_index,
                    event_ordinal=ordinal_base + 1,
                    turn_id=mid or None,
                    call_id=tool.call_id,
                    parent_call_id=tool.parent_id,
                    mcp_server=mcp_server,
                    mcp_tool=mcp_tool,
                    failure=tool.result_failure,
                    timing=tool.timing,
                    request_id=request_id if role == "assistant" else None,
                    details=tool.result_details,
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
        previous_state = self._trajectory_state.get(key)
        if previous_state is not None:
            previous_revision, previous = previous_state
            self._trajectory_state.move_to_end(key)
            if previous == candidate:
                return None
        else:
            previous_revision = -1
        revision = max(previous_revision + 1, revision_hint or 0)
        self._trajectory_state[key] = (revision, candidate)
        while len(self._trajectory_state) > LIVE_TRAJECTORY_STATE_LIMIT:
            self._trajectory_state.popitem(last=False)
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
            row = queries.live_revision_row(conn, table, record_id)
            if row is not None:
                values[:0] = row
        revisions = [self._history_revision(value, 0) for value in values]
        return max(revisions, default=0)

    def _message_coordinate(
        self, conn: sqlite3.Connection, message_id: object, fallback: int
    ) -> int:
        if isinstance(message_id, str) and message_id:
            row = queries.message_coordinate(conn, message_id)
            if row is not None:
                return self._history_coordinate(row[0])
        return max(0, fallback)

    def _trajectory_for_part(
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
        ordinal_base = queries.part_ordinal(conn, fallback) * 2 if fallback else 0
        revision_hint = self._live_revision(
            conn,
            "part",
            fallback,
            payload.get("time"),
            seq,
        )
        step_id = part.get("stepID") or part.get("stepId")
        common = {
            "native_id": part_id,
            "fallback_id": fallback,
            "raw_index": raw_index,
            "event_ordinal": ordinal_base,
            "turn_id": message_id or None,
            "step_id": step_id if isinstance(step_id, str) else None,
            "timing": timing,
            "revision_hint": revision_hint,
        }
        if ptype == "tool":
            return self._tool_part_facts(part, request_id=request_id, **common)
        return self._simple_part_facts(
            part, ptype=ptype, role=role, request_id=request_id, **common
        )

    def _simple_part_facts(
        self,
        part: dict,
        *,
        ptype: object,
        role: str | None,
        request_id: str | None,
        **common,
    ) -> list[TrajectoryFact]:
        if ptype == "text":
            classification = (
                {
                    "assistant": (TrajectoryKind.ASSISTANT, TrajectoryStatus.RUNNING),
                    "user": (TrajectoryKind.USER, TrajectoryStatus.COMPLETED),
                    "system": (TrajectoryKind.SYSTEM, TrajectoryStatus.COMPLETED),
                    "developer": (TrajectoryKind.SYSTEM, TrajectoryStatus.COMPLETED),
                }.get(role)
                if role is not None
                else None
            )
            if classification is None:
                return []
            kind, status = classification
            fact = self._live_fact(
                kind=kind,
                summary=_trajectory_string(part.get("text")),
                status=status,
                request_id=request_id,
                **common,
            )
        elif ptype in ("reasoning", "thinking"):
            timing = common["timing"]
            status = (
                TrajectoryStatus.COMPLETED
                if timing is not None and timing.end is not None
                else TrajectoryStatus.RUNNING
            )
            fact = self._live_fact(
                kind=TrajectoryKind.REASONING,
                summary=_trajectory_string(part.get("text")),
                status=status,
                request_id=request_id,
                **common,
            )
        elif ptype in ("context", "system"):
            fact = self._live_fact(
                kind=TrajectoryKind.SYSTEM if ptype == "system" else TrajectoryKind.CONTEXT,
                summary=_trajectory_text(part.get("text") or part.get("content")),
                status=TrajectoryStatus.COMPLETED,
                **common,
            )
        else:
            return []
        return [fact] if fact is not None else []

    def _tool_part_facts(
        self,
        part: dict,
        *,
        request_id: str | None,
        **common,
    ) -> list[TrajectoryFact]:
        tool = _interpret_tool_part(part)
        mcp_identity = self._opencode_mcp_identity(tool.name)
        mcp_server, mcp_tool = mcp_identity or (None, None)
        call_common = {**common, "native_id": tool.call_id}
        call_fact = self._live_fact(
            kind=TrajectoryKind.TOOL_CALL,
            summary=tool.name or "",
            status=tool.call_status,
            request_id=request_id,
            call_id=tool.call_id,
            parent_call_id=tool.parent_id,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
            details=tool.call_details,
            **call_common,
        )
        facts = [call_fact] if call_fact is not None else []
        if tool.result_status is not None and tool.result_text is not None:
            result_fact = self._live_fact(
                kind=TrajectoryKind.TOOL_RESULT,
                summary=tool.result_text,
                status=tool.result_status,
                native_id=tool.result_id,
                fallback_id=tool.result_fallback_id,
                raw_index=common["raw_index"],
                event_ordinal=common["event_ordinal"] + 1,
                turn_id=common["turn_id"],
                step_id=common["step_id"],
                request_id=request_id,
                call_id=tool.call_id,
                parent_call_id=tool.parent_id,
                mcp_server=mcp_server,
                mcp_tool=mcp_tool,
                failure=tool.result_failure,
                timing=common["timing"],
                details=tool.result_details,
                revision_hint=common["revision_hint"],
            )
            if result_fact is not None:
                facts.append(result_fact)
        return facts

    def _opencode_mcp_identity(self, value: object) -> tuple[str, str] | None:
        return self._mcp_catalog.identity(value)

    def _refresh_mcp_trajectory(self) -> tuple[TrajectoryFact, ...]:
        generation = self._mcp_catalog.generation
        if generation == self._mcp_catalog_generation:
            return ()
        self._mcp_catalog_generation = generation
        identities: dict[str, tuple[str, str]] = {}
        for _key, (_revision, fact) in self._trajectory_state.items():
            if fact.kind is not TrajectoryKind.TOOL_CALL or fact.call_id is None:
                continue
            identity = (
                (fact.mcp_server, fact.mcp_tool)
                if fact.mcp_server is not None and fact.mcp_tool is not None
                else self._opencode_mcp_identity(fact.summary)
            )
            if identity is not None:
                identities[fact.call_id] = identity
        updates: list[TrajectoryFact] = []
        for key, (revision, fact) in tuple(self._trajectory_state.items()):
            if fact.mcp_server is not None or fact.call_id is None:
                continue
            identity = identities.get(fact.call_id)
            if identity is None:
                continue
            candidate = replace(fact, mcp_server=identity[0], mcp_tool=identity[1])
            next_revision = revision + 1
            self._trajectory_state[key] = (next_revision, candidate)
            updates.append(replace(candidate, revision=next_revision))
        return tuple(updates)

    def _trajectory_for_message(
        self,
        conn: sqlite3.Connection,
        payload: dict,
        seq: int,
        *,
        raw_index: int,
        has_tool_calls: bool = False,
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
                status = _finish_status(finish, has_tool_calls)
                for part_id, part_text in text_parts.items():
                    fact = self._live_fact(
                        kind=TrajectoryKind.ASSISTANT,
                        summary=part_text,
                        status=status,
                        native_id=part_id,
                        fallback_id=f"{mid}:text" if mid else None,
                        raw_index=raw_index,
                        event_ordinal=queries.part_ordinal(conn, part_id) * 2,
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
                    status=_finish_status(finish, has_tool_calls),
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
