"""OpenCode trajectory and usage projection."""

# mypy: disable-error-code=attr-defined

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness.base import SERVER_NAME, EventPath, TokenUsage
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

from .constants import _WRITE_TOOLS, OPENCODE_MODEL_ID_KEY, OPENCODE_PROVIDER_ID_KEY, STEP_FINISH
from .store import live_revision_row, message_coordinate


def _seconds(ms) -> float | None:
    """Milliseconds to a unix epoch float. None for anything else."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return None
    return ms / 1000.0


def _loads(raw) -> dict:
    """A JSON column as a dict. Empty for anything that is not one.

    Rows are read from under a live writer, so a value that does not parse is
    an expected condition rather than a corruption to report.
    """
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
    """A nested object inside an already-parsed row. Empty for anything else.

    Written as a function rather than inline so the value is tested once:
    `x.get(k) if isinstance(x.get(k), dict) else {}` reads the key twice and
    leaves the result typed as the union it was before the test.
    """
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


def _relativise(path: str, cwd: str | None) -> str | None:
    """Make a path repo-relative, or None if it cannot be done safely.

    opencode's `filePath` may be absolute or relative to the session's
    working directory (write.ts:41-43, edit.ts:80-82 resolve it against
    `instance.directory`). We relativise against `cwd`, which is the
    directory the source was constructed with — the same value the daemon
    uses to locate the session row. A path already relative is returned
    unchanged, on the assumption that it is already repo-relative; this is
    correct for opencode, which resolves relative paths against the session
    directory at execution time and stores them as given.

    Both sides are resolved before comparison, because macOS aliases
    ``/tmp`` as ``/private/tmp`` and a mismatch there would drop a path that
    is genuinely inside the repo. The session directory in the database is
    also stored resolved (see ``_locate``), so this is consistent with how
    the source already treats paths.

    None is returned when the path is outside the repo root, because an
    absolute path that does not start with `cwd` is either a temp file or a
    path into another project — both of which would pollute the index with
    false entries. Better to record nothing than to record a wrong path.
    """
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        return path
    if cwd is None:
        # Returning the absolute path would leak a home directory into the index; None drops it.
        return None
    try:
        rel = p.resolve().relative_to(Path(cwd).resolve())
    except (ValueError, OSError):
        return None
    return str(rel)


def _paths_from_tool(name: str, state: dict, cwd: str | None) -> tuple[EventPath, ...]:
    """Extract file paths from a tool call's structured input.

    Only `state.input` is read — the decoded JSON arguments the LLM passed.
    Paths are never parsed out of shell command strings or patch text; the
    contract is that a wrong path is worse than a missing one, and parsing
    prose or commands is exactly where wrong paths come from.

    `glob` and `grep` take a `path` field, but it is a directory to search
    within, not a file. Per the design, a search over a directory yields no
    paths. `apply_patch` embeds paths inside a `patchText` string, which is
    the same class of unstructured input we decline to parse. `bash`/`shell`
    has no file path field in its structured input at all.
    """
    if not name or name in ("bash", "shell", "apply_patch", "glob", "grep", "webfetch"):
        return ()
    input_data = state.get("input")
    if not isinstance(input_data, dict):
        return ()
    raw = input_data.get("filePath")
    if not isinstance(raw, str):
        return ()
    rel = _relativise(raw, cwd)
    if rel is None:
        return ()
    mode: Literal["read", "write"] = "write" if name in _WRITE_TOOLS else "read"
    return (EventPath(path=rel, mode=mode),)


class OpenCodeTrajectory:
    def _stored_fact(
        self,
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
        native = _trajectory_identifier(native_id, "native")
        if native is None:
            native = _trajectory_identifier(fallback_id, "fallback")
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
            part = _loads(raw)
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
                    self._stored_fact(
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
                self._stored_fact(
                    kind=TrajectoryKind.ASSISTANT,
                    summary="",
                    status=self._finish_status(finish),
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
                status = self._finish_status(info.get("finish"))
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
                self._stored_fact(
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
                else self._finish_status(info.get("finish"))
            )
            return [
                self._stored_fact(
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
                self._stored_fact(
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
            self._stored_fact(
                kind=TrajectoryKind.TOOL_CALL,
                summary=tool_name or "",
                status=self._tool_status(state_status),
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
                self._stored_fact(
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
                    failure=(
                        TrajectoryFailure(TrajectoryFailureCategory.TOOL, detail=result)
                        if state_status == "error"
                        else None
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

    @staticmethod
    def _finish_status(finish: object) -> TrajectoryStatus:
        if not finish:
            return TrajectoryStatus.RUNNING
        return TrajectoryStatus.PARTIAL if finish == STEP_FINISH else TrajectoryStatus.COMPLETED

    @staticmethod
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
            status=self._tool_status(state_status),
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
                status = self._finish_status(finish)
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
                    status=self._finish_status(finish),
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
