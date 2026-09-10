"""Codex rollout parsing and event normalization."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, BinaryIO

from theater.constants.trajectory import (
    TRAJECTORY_MCP_CALL_CONTEXT_LIMIT,
    TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES,
)
from theater.harness.base import Event, EventKind, EventPath, TokenUsage, clipper
from theater.harness.contracts.trajectory import ParsedRecord
from theater.harness.normalization.timing import iso_epoch as _epoch
from theater.harness.normalization.usage import reported_cost
from theater.harness.normalization.values import (
    decode_json_record,
)
from theater.harness.normalization.values import (
    finite_float as _trajectory_float,
)
from theater.harness.normalization.values import (
    nonnegative_int as _usage_int,
)

from .constants import (
    _CWD_PROBE_BYTES,
    CODEX_MODEL_PROVIDER_ID_KEY,
    CODEX_MODEL_PROVIDER_KEY,
    CODEX_SESSION_META_RECORD_TYPE,
    CODEX_THREAD_SETTINGS_EVENT_TYPE,
)
from .paths import _apply_patch_paths, _patch_change_paths
from .values import (
    _codex_mcp_identity,
    _codex_timing,
    _codex_trajectory_turn_id,
    _flatten,
    _trajectory_id,
    _turn_id,
)

if TYPE_CHECKING:
    from theater.harness.contracts.trajectory import TrajectoryFact


def _trajectory_only_event(payload: dict) -> bool:
    """Whether a legacy or paginated message event is control-only.

    ``user_message``/``agent_message`` and the paginated
    ``item_completed`` UserMessage/AgentMessage items exist to be heard and
    said; the trajectory projection takes the same words from the raw
    ``response_item`` message facts instead, so projecting these events
    would duplicate every message in the canonical records.
    """
    event_type = payload.get("type")
    if event_type in {"user_message", "agent_message"}:
        return True
    if event_type == "item_completed":
        item = payload.get("item")
        return isinstance(item, dict) and item.get("type") in {"UserMessage", "AgentMessage"}
    return False


class CodexParserMixin:
    if TYPE_CHECKING:
        _active_turn_id: str | None
        _last_cwd: str | None
        _last_model: str | None
        _last_provider: str | None
        _mcp_calls: dict[str, tuple[str, str]]
        _pending_patch_exec: tuple[str, float] | None

        def _trajectory_facts(self, record: dict, index: int) -> list[TrajectoryFact]: ...

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        record = self._decode(line)
        if record is None:
            return []
        return self._parse_decoded(record, index, clip_text=clip_text)

    @staticmethod
    def _decode(line: str) -> dict | None:
        return decode_json_record(line)

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        record = self._decode(line)
        if record is None:
            return ParsedRecord()
        events = tuple(self._parse_decoded(record, index, clip_text=clip_text))
        payload = record.get("payload")
        redundant = (
            record.get("type") == "event_msg"
            and isinstance(payload, dict)
            and _trajectory_only_event(payload)
        )
        return ParsedRecord(
            events=events,
            trajectory=tuple(self._trajectory_facts(record, index)),
            trajectory_events=() if redundant else None,
        )

    def _parse_decoded(self, record: dict, index: int, *, clip_text: bool = True) -> list[Event]:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return []

        ts = _epoch(record.get("timestamp"))
        kind = record.get("type")
        self._remember_context(record, payload)
        if kind == "event_msg":
            return self._event(payload, ts, index, clip_text=clip_text)
        if kind == "response_item":
            return self._item(payload, ts, index, clip_text=clip_text)
        return []

    def _remember_context(self, record: dict, payload: dict) -> None:
        self._remember_patch_exec(record, payload)
        self._remember_mcp_call(record, payload)
        turn_id = _codex_trajectory_turn_id(payload)
        if turn_id is not None:
            self._active_turn_id = turn_id

        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            self._last_cwd = cwd

        kind = record.get("type")
        if kind == CODEX_SESSION_META_RECORD_TYPE:
            provider = payload.get(CODEX_MODEL_PROVIDER_KEY) or payload.get(
                CODEX_MODEL_PROVIDER_ID_KEY
            )
            if isinstance(provider, str) and provider:
                self._last_provider = provider

        settings = None
        if kind == "turn_context":
            settings = payload
        elif kind == CODEX_THREAD_SETTINGS_EVENT_TYPE:
            settings = payload.get("thread_settings") or payload
        elif kind == "event_msg" and payload.get("type") == CODEX_THREAD_SETTINGS_EVENT_TYPE:
            settings = payload.get("thread_settings")
        if not isinstance(settings, dict):
            return
        model = settings.get("model") or settings.get("model_name")
        if isinstance(model, str) and model:
            self._last_model = model
        provider = settings.get(CODEX_MODEL_PROVIDER_ID_KEY) or settings.get(
            CODEX_MODEL_PROVIDER_KEY
        )
        if isinstance(provider, str) and provider:
            self._last_provider = provider

    def _remember_mcp_call(self, record: dict, payload: dict) -> None:
        record_kind = record.get("type")
        payload_kind = payload.get("type")
        if record_kind == "event_msg" and payload_kind in {
            "mcp_tool_call_begin",
            "mcp_tool_call_end",
        }:
            identity = _codex_mcp_identity(payload.get("invocation"))
        elif record_kind == "response_item" and payload_kind == "mcp_tool_call":
            identity = _codex_mcp_identity(payload)
        else:
            return
        call_id = _trajectory_id(payload.get("call_id"))
        if identity is None or call_id is None:
            return
        self._mcp_calls[call_id] = identity
        while len(self._mcp_calls) > TRAJECTORY_MCP_CALL_CONTEXT_LIMIT:
            self._mcp_calls.pop(next(iter(self._mcp_calls)))

    def _remember_patch_exec(self, record: dict, payload: dict) -> None:
        if record.get("type") != "response_item":
            return
        item_type = payload.get("type")
        call_id = _trajectory_id(payload.get("call_id"))
        if item_type in {"custom_tool_call", "function_call"}:
            input_value = payload.get("input")
            if input_value is None:
                input_value = payload.get("arguments")
            timing = _codex_timing(record, payload, _epoch(record.get("timestamp")))
            self._pending_patch_exec = (
                (call_id, timing.start)
                if payload.get("name") in {"exec", "exec_command"}
                and isinstance(input_value, str)
                and "tools.apply_patch" in input_value
                and call_id is not None
                and timing is not None
                and timing.start is not None
                else None
            )
        elif item_type in {"custom_tool_call_output", "function_call_output"}:
            if self._pending_patch_exec is not None and call_id == self._pending_patch_exec[0]:
                self._pending_patch_exec = None

    def _seed_history_context(self, fh: BinaryIO, start: int) -> None:
        self._active_turn_id = None
        self._last_model = None
        self._last_provider = None
        self._last_cwd = None
        self._pending_patch_exec = None
        self._mcp_calls.clear()

        fh.seek(0)
        first_line = fh.readline(min(_CWD_PROBE_BYTES, max(0, start)))
        first_record = self._decode(first_line.decode("utf-8", errors="replace"))
        if first_record is not None:
            payload = first_record.get("payload")
            if isinstance(payload, dict):
                self._remember_context(first_record, payload)

        scan_start = max(0, start - TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES)
        fh.seek(scan_start)
        context = fh.read(start - scan_start)
        if scan_start:
            _, separator, context = context.partition(b"\n")
            if not separator:
                return
        for raw in context.splitlines():
            record = self._decode(raw.decode("utf-8", errors="replace"))
            if record is None:
                continue
            payload = record.get("payload")
            if isinstance(payload, dict):
                self._remember_context(record, payload)

    def _event(
        self, payload: dict, ts: float | None, index: int, *, clip_text: bool
    ) -> list[Event]:
        _clip = clipper(clip_text)
        ptype = payload.get("type")

        if ptype == "user_message":
            raw = payload.get("message") if isinstance(payload.get("message"), str) else ""
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "agent_message":
            if payload.get("phase") == "final_answer":
                return []
            raw = payload.get("message") if isinstance(payload.get("message"), str) else ""
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "task_complete":
            raw = (
                payload.get("last_agent_message")
                if isinstance(payload.get("last_agent_message"), str)
                else ""
            )
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    turn_end=True,
                    turn_id=_turn_id(payload),
                    raw_index=index,
                )
            ]
        if ptype == "turn_aborted":
            raw = f"turn aborted: {payload.get('reason') or 'unknown'}"
            return [
                Event(
                    kind=EventKind.ERROR,
                    text=raw,
                    raw_text=raw,
                    ts=ts,
                    turn_end=True,
                    turn_id=_turn_id(payload),
                    raw_index=index,
                )
            ]
        if ptype == "patch_apply_end":
            paths = _patch_change_paths(payload.get("changes"), cwd=self._last_cwd)
            if not paths:
                return []
            return [
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name="apply_patch",
                    ts=ts,
                    raw_index=index,
                    paths=paths,
                )
            ]
        if ptype in ("mcp_tool_call_begin", "mcp_tool_call_end"):
            invocation = payload.get("invocation")
            invocation = invocation if isinstance(invocation, dict) else {}
            tool_name = ".".join(
                str(part) for part in (invocation.get("server"), invocation.get("tool")) if part
            )
            if ptype == "mcp_tool_call_begin":
                return [
                    Event(
                        kind=EventKind.TOOL_CALL,
                        tool_name=tool_name or None,
                        ts=ts,
                        raw_index=index,
                    )
                ]
            raw = self._mcp_result(payload.get("result"))
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(raw),
                    raw_text=raw,
                    tool_name=tool_name or None,
                    ts=ts,
                    raw_index=index,
                )
            ]
        if ptype == "item_completed":
            return self._item_completed_events(payload, ts, index, clip_text=clip_text)
        if ptype == "token_count":
            return self._token_count(payload, ts, index)
        return []

    def _mcp_result(self, result) -> str:
        if not isinstance(result, dict):
            return "" if result is None else json.dumps(result, default=str)
        ok = result.get("Ok")
        if isinstance(ok, dict):
            return _flatten(ok.get("content"))
        err = result.get("Err")
        if err is not None:
            return err if isinstance(err, str) else json.dumps(err, default=str)
        return json.dumps(result, default=str)

    @staticmethod
    def _item_text(content: object) -> str:
        """Join the text blocks of a paginated UserMessage/AgentMessage item.

        Wire shapes come from the native items: user content blocks carry
        `{"type": "text", "text": ...}` and agent content blocks carry
        `{"type": "Text", "text": ...}` — non-text blocks (images, audio,
        structured mentions) have no "text" field and stay silent.
        """
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if str(block.get("type") or "").lower() != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n".join(parts)

    def _item_completed_events(
        self, payload: dict, ts: float | None, index: int, *, clip_text: bool
    ) -> list[Event]:
        """Modern paginated history: the durable UI items per turn.

        Codex's paginated rollouts persist ``item_completed`` events and
        drop the legacy ``user_message``/``agent_message``/
        ``mcp_tool_call_*``/``patch_apply_end`` events, so these items are
        the only place the user prompt is visible to the prompt gate.
        Kinds the raw ``response_item`` records already cover (exec calls,
        reasoning, function outputs) stay silent here — emitting them would
        duplicate every message and tool in the turn.
        """
        _clip = clipper(clip_text)
        item = payload.get("item")
        if not isinstance(item, dict):
            return []
        item_type = item.get("type")
        turn_id = _codex_trajectory_turn_id(payload)

        if item_type == "UserMessage":
            raw = self._item_text(item.get("content"))
            if not raw:
                return []
            return [
                Event(
                    kind=EventKind.USER,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                    turn_id=turn_id,
                )
            ]
        if item_type == "AgentMessage":
            if item.get("phase") == "final_answer":
                return []
            raw = self._item_text(item.get("content"))
            if not raw:
                return []
            return [
                Event(
                    kind=EventKind.ASSISTANT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                    turn_id=turn_id,
                )
            ]
        if item_type == "McpToolCall":
            return self._mcp_item_events(item, ts, index, clip_text=clip_text, turn_id=turn_id)
        if item_type == "FileChange":
            paths = _patch_change_paths(item.get("changes"), cwd=self._last_cwd)
            if not paths:
                return []
            return [
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name="apply_patch",
                    ts=ts,
                    raw_index=index,
                    turn_id=turn_id,
                    paths=paths,
                )
            ]
        return []

    def _mcp_item_events(
        self, item: dict, ts: float | None, index: int, *, clip_text: bool, turn_id: str | None
    ) -> list[Event]:
        """A completed MCP call: one ``CallToolResult`` merged into the item.

        Mirrors the legacy ``mcp_tool_call_begin``/``mcp_tool_call_end`` pair
        the paginated history no longer persists.
        """
        _clip = clipper(clip_text)
        identity = _codex_mcp_identity(item)
        server, tool = identity or (None, None)
        tool_name = ".".join(str(part) for part in (server, tool) if part) or None
        events = [
            Event(
                kind=EventKind.TOOL_CALL,
                tool_name=tool_name,
                ts=ts,
                raw_index=index,
                turn_id=turn_id,
            )
        ]
        error = item.get("error")
        result = item.get("result")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            raw = error["message"]
        elif isinstance(result, dict) and isinstance(result.get("content"), list):
            raw = _flatten(result["content"])
        elif result is not None:
            raw = self._mcp_result(result)
        else:
            raw = ""
        events.append(
            Event(
                kind=EventKind.TOOL_RESULT,
                text=_clip(raw),
                raw_text=raw or None,
                tool_name=tool_name,
                ts=ts,
                raw_index=index,
                turn_id=turn_id,
            )
        )
        return events

    def _token_count(self, payload: dict, ts: float | None, index: int) -> list[Event]:
        info = payload.get("info")
        if not isinstance(info, dict):
            return []
        last = info.get("last_token_usage")
        if not isinstance(last, dict):
            return []
        total = info.get("total_token_usage") or {}
        if not isinstance(total, dict):
            return []
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
        totals = tuple(_usage_int(total.get(field)) for field in fields)
        latest = tuple(_usage_int(last.get(field)) for field in fields)
        model = info.get("model") or info.get("model_name") or self._last_model
        model = model or None if isinstance(model, str) else None
        input_tokens, cache_read, cache_write, output_tokens, reasoning = latest
        cost = _trajectory_float(
            last.get("cost_usd") if "cost_usd" in last else last.get("costUSD")
        )
        cost, cost_provenance = reported_cost(cost, strict_positive=False)
        usage = TokenUsage(
            model=model,
            provider=self._last_provider,
            input_tokens=max(0, input_tokens - cache_read - cache_write),
            output_tokens=max(0, output_tokens - reasoning),
            cache_creation_input_tokens=cache_write,
            cache_read_input_tokens=cache_read,
            reasoning_output_tokens=reasoning,
            cost_usd=cost,
            cost_provenance=cost_provenance,
            idempotency_key="codex:" + ":".join(str(value) for value in totals + latest),
        )
        if (
            usage.input_tokens == 0
            and usage.output_tokens == 0
            and usage.cache_creation_input_tokens == 0
            and usage.cache_read_input_tokens == 0
            and usage.reasoning_output_tokens == 0
        ):
            return []
        return [Event(kind=EventKind.ASSISTANT, ts=ts, raw_index=index, usage=usage)]

    def _item(self, payload: dict, ts: float | None, index: int, *, clip_text: bool) -> list[Event]:
        _clip = clipper(clip_text)
        ptype = payload.get("type")

        if ptype in ("custom_tool_call", "function_call"):
            name = payload.get("name")
            paths: tuple[EventPath, ...] = ()
            if name == "apply_patch":
                raw_input = payload.get("input")
                paths = _apply_patch_paths(
                    raw_input if isinstance(raw_input, str) else "",
                    cwd=self._last_cwd,
                )
            return [
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=name,
                    ts=ts,
                    raw_index=index,
                    paths=paths,
                )
            ]
        if ptype in ("custom_tool_call_output", "function_call_output"):
            raw = _flatten(payload.get("output"))
            return [
                Event(
                    kind=EventKind.TOOL_RESULT,
                    text=_clip(raw),
                    raw_text=raw,
                    ts=ts,
                    raw_index=index,
                )
            ]
        return []
