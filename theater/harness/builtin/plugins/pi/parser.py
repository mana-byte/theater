"""Pi JSONL record decoding, turn boundaries, and trajectory projection."""

from __future__ import annotations

from dataclasses import replace
from typing import BinaryIO

from theater.constants.trajectory import TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES
from theater.harness.contracts.events import Event, EventKind, TokenUsage, clipper
from theater.harness.contracts.trajectory import ParsedRecord, TrajectoryFact
from theater.harness.normalization.facts import fact_builder, tool_failure
from theater.harness.normalization.timing import iso_epoch
from theater.harness.normalization.usage import reported_cost, trajectory_usage_from_token_usage
from theater.harness.normalization.values import (
    decode_json_record,
    json_container_format,
    nonnegative_int,
    optional_trajectory_detail,
    trajectory_identifier,
)
from theater.trajectory.content import ContentFormat
from theater.trajectory.enums import TimingProvenance, TrajectoryKind, TrajectoryStatus
from theater.trajectory.records import Timing

_pi_fact = fact_builder(
    source="pi",
    identifier=lambda value: trajectory_identifier(value, overflow_prefix="pi"),
)
_TERMINAL_STOPS = {"stop", "length", "error", "aborted"}


def _record_id(record: dict) -> str | None:
    return trajectory_identifier(record.get("id"), overflow_prefix="pi")


def _timestamp(record: dict, message: dict | None = None) -> float | None:
    if isinstance(message, dict):
        value = message.get("timestamp")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) / 1_000
    return iso_epoch(record.get("timestamp"))


def _content_text(value: object) -> str:
    """Flatten text blocks without serializing image payloads into the bus."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        block["text"]
        for block in value
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _blocks(value: object, block_type: str) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [block for block in value if isinstance(block, dict) and block.get("type") == block_type]


def _usage(message: dict, entry_id: str | None, index: int) -> TokenUsage | None:
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return None
    total_cost = raw.get("cost")
    total_cost = total_cost.get("total") if isinstance(total_cost, dict) else None
    cost, provenance = reported_cost(total_cost, strict_positive=False)
    return TokenUsage(
        model=message.get("model") if isinstance(message.get("model"), str) else None,
        provider=message.get("provider") if isinstance(message.get("provider"), str) else None,
        input_tokens=nonnegative_int(raw.get("input")),
        output_tokens=nonnegative_int(raw.get("output")),
        cache_creation_input_tokens=nonnegative_int(raw.get("cacheWrite")),
        cache_read_input_tokens=nonnegative_int(raw.get("cacheRead")),
        reasoning_output_tokens=nonnegative_int(raw.get("reasoning")),
        cost_usd=cost,
        cost_provenance=provenance,
        # Pi normally assigns an entry id.  An append-only record coordinate
        # remains deterministic when it does not, which keeps restart
        # reconciliation idempotent instead of silently double-counting.
        idempotency_key=entry_id or f"record:{index}",
    )


def _timing(timestamp: float | None) -> Timing | None:
    return (
        Timing(end=timestamp, provenance=TimingProvenance.SOURCE) if timestamp is not None else None
    )


class PiParserMixin:
    _active_turn_id: str | None
    _last_model: str | None
    _last_provider: str | None

    def _reset_turn_context(self) -> None:
        self._active_turn_id = None
        self._last_model = None
        self._last_provider = None

    def _seed_history_context(self, stream: BinaryIO, start: int) -> None:
        self._reset_turn_context()
        scan_start = max(0, start - TRAJECTORY_TRANSCRIPT_HISTORY_MAX_SCAN_BYTES)
        stream.seek(scan_start)
        data = stream.read(start - scan_start)
        if scan_start:
            _prefix, separator, data = data.partition(b"\n")
            if not separator:
                return
        for raw in data.splitlines():
            record = decode_json_record(raw)
            if record is not None:
                self._remember(record)

    def parse(self, line: str, index: int, *, clip_text: bool = True) -> list[Event]:
        return list(self.parse_record(line, index, clip_text=clip_text).events)

    def parse_record(self, line: str, index: int, *, clip_text: bool = True) -> ParsedRecord:
        record = decode_json_record(line)
        if record is None:
            return ParsedRecord()
        return self._parsed_record(record, index, clip_text=clip_text)

    def _parsed_record(self, record: dict, index: int, *, clip_text: bool) -> ParsedRecord:
        record_type = record.get("type")
        entry_id = _record_id(record)
        timestamp = _timestamp(record)
        if record_type == "session":
            return ParsedRecord()
        if record_type == "model_change":
            model = record.get("modelId") if isinstance(record.get("modelId"), str) else None
            provider = record.get("provider") if isinstance(record.get("provider"), str) else None
            self._last_model, self._last_provider = model, provider
            return ParsedRecord(
                trajectory=(
                    _pi_fact(
                        kind=TrajectoryKind.CONTEXT,
                        summary=f"model: {model or 'unknown'}",
                        native_id=entry_id,
                        raw_index=index,
                        timing=_timing(timestamp),
                        details=tuple(
                            detail
                            for detail in (
                                optional_trajectory_detail("model", model),
                                optional_trajectory_detail("provider", provider),
                            )
                            if detail is not None
                        ),
                    ),
                ),
                trajectory_events=(),
            )
        if record_type == "thinking_level_change":
            level = (
                record.get("thinkingLevel") if isinstance(record.get("thinkingLevel"), str) else ""
            )
            return ParsedRecord(
                trajectory=(
                    _pi_fact(
                        kind=TrajectoryKind.CONTEXT,
                        summary=f"thinking: {level or 'unknown'}",
                        native_id=entry_id,
                        raw_index=index,
                        timing=_timing(timestamp),
                        details=tuple(
                            detail
                            for detail in (optional_trajectory_detail("thinking", level),)
                            if detail is not None
                        ),
                    ),
                ),
                trajectory_events=(),
            )
        if record_type in {"compaction", "branch_summary"}:
            return self._summary_record(record, index, entry_id, timestamp)
        if record_type != "message" or not isinstance((message := record.get("message")), dict):
            return ParsedRecord()
        return self._message_record(record, message, index, entry_id, clip_text)

    def _remember(self, record: dict) -> None:
        record_type = record.get("type")
        if record_type == "model_change":
            self._last_model = (
                record.get("modelId") if isinstance(record.get("modelId"), str) else None
            )
            self._last_provider = (
                record.get("provider") if isinstance(record.get("provider"), str) else None
            )
            return
        if record_type != "message" or not isinstance((message := record.get("message")), dict):
            return
        role = message.get("role")
        if role == "user":
            self._active_turn_id = _record_id(record)
        elif role == "assistant" and not _blocks(message.get("content"), "toolCall"):
            self._active_turn_id = None
        if role == "assistant":
            self._last_model = (
                message.get("model") if isinstance(message.get("model"), str) else None
            )
            self._last_provider = (
                message.get("provider") if isinstance(message.get("provider"), str) else None
            )

    def _message_record(
        self,
        record: dict,
        message: dict,
        index: int,
        entry_id: str | None,
        clip_text: bool,
    ) -> ParsedRecord:
        role = message.get("role")
        timestamp = _timestamp(record, message)
        if role == "user":
            turn_id = entry_id
            self._active_turn_id = turn_id
            raw = _content_text(message.get("content"))
            fact = _pi_fact(
                kind=TrajectoryKind.USER,
                summary=raw,
                native_id=entry_id,
                raw_index=index,
                turn_id=turn_id,
                timing=_timing(timestamp),
            )
            return ParsedRecord(
                events=(
                    Event(
                        kind=EventKind.USER,
                        text=clipper(clip_text)(raw),
                        raw_text=raw,
                        ts=timestamp,
                        turn_id=turn_id,
                        raw_index=index,
                    ),
                )
                if raw
                else (),
                trajectory=(fact,),
                trajectory_events=(),
            )
        if role == "assistant":
            return self._assistant_record(record, message, index, entry_id, timestamp, clip_text)
        if role == "toolResult":
            return self._tool_result_record(record, message, index, entry_id, timestamp, clip_text)
        return ParsedRecord()

    def _assistant_record(
        self,
        record: dict,
        message: dict,
        index: int,
        entry_id: str | None,
        timestamp: float | None,
        clip_text: bool,
    ) -> ParsedRecord:
        del record
        turn_id = self._active_turn_id
        raw = _content_text(message.get("content"))
        calls = _blocks(message.get("content"), "toolCall")
        thinking = _blocks(message.get("content"), "thinking")
        usage = _usage(message, entry_id, index)
        self._last_model = message.get("model") if isinstance(message.get("model"), str) else None
        self._last_provider = (
            message.get("provider") if isinstance(message.get("provider"), str) else None
        )
        stop_reason = message.get("stopReason")
        terminal = not calls and stop_reason in _TERMINAL_STOPS
        events: list[Event] = []
        if raw:
            events.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=clipper(clip_text)(raw),
                    raw_text=raw,
                    ts=timestamp,
                    turn_id=turn_id,
                    raw_index=index,
                )
            )
        for call in calls:
            name = call.get("name") if isinstance(call.get("name"), str) else None
            events.append(
                Event(
                    kind=EventKind.TOOL_CALL,
                    tool_name=name,
                    ts=timestamp,
                    turn_id=turn_id,
                    raw_index=index,
                )
            )
        if usage is not None:
            events.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    ts=timestamp,
                    turn_id=turn_id,
                    raw_index=index,
                    usage=usage,
                )
            )
        error = message.get("errorMessage") if isinstance(message.get("errorMessage"), str) else ""
        if terminal and error and not raw:
            events.append(
                Event(
                    kind=EventKind.ERROR,
                    text=clipper(clip_text)(error),
                    raw_text=error,
                    ts=timestamp,
                    turn_id=turn_id,
                    raw_index=index,
                )
            )
        if terminal:
            if events:
                events[-1] = replace(events[-1], turn_end=True)
            else:
                events.append(
                    Event(
                        kind=EventKind.ASSISTANT,
                        ts=timestamp,
                        turn_end=True,
                        turn_id=turn_id,
                        raw_index=index,
                    )
                )
            self._active_turn_id = None

        facts: list[TrajectoryFact] = [
            _pi_fact(
                kind=TrajectoryKind.ASSISTANT,
                summary=raw or error,
                native_id=entry_id,
                raw_index=index,
                turn_id=turn_id,
                timing=_timing(timestamp),
                status=(
                    TrajectoryStatus.ERROR
                    if stop_reason == "error"
                    else TrajectoryStatus.INTERRUPTED
                    if stop_reason == "aborted"
                    else TrajectoryStatus.COMPLETED
                    if terminal
                    else TrajectoryStatus.UNKNOWN
                ),
                details=tuple(
                    detail
                    for detail in (
                        optional_trajectory_detail("stop_reason", stop_reason),
                        optional_trajectory_detail("error", error),
                    )
                    if detail is not None
                ),
            )
        ]
        for block in thinking:
            text = _content_text([block])
            if text:
                facts.append(
                    _pi_fact(
                        kind=TrajectoryKind.REASONING,
                        summary=text,
                        native_id=f"{entry_id}:thinking:{len(facts)}" if entry_id else None,
                        raw_index=index,
                        event_ordinal=len(facts),
                        turn_id=turn_id,
                        timing=_timing(timestamp),
                    )
                )
        for call in calls:
            call_id = call.get("id") if isinstance(call.get("id"), str) else None
            name = call.get("name") if isinstance(call.get("name"), str) else None
            arguments = call.get("arguments")
            facts.append(
                _pi_fact(
                    kind=TrajectoryKind.TOOL_CALL,
                    summary=name or "",
                    native_id=call_id,
                    raw_index=index,
                    event_ordinal=len(facts),
                    turn_id=turn_id,
                    call_id=call_id,
                    status=TrajectoryStatus.UNKNOWN,
                    timing=_timing(timestamp),
                    details=tuple(
                        detail
                        for detail in (
                            optional_trajectory_detail("tool", name),
                            optional_trajectory_detail(
                                "arguments",
                                arguments,
                                format=json_container_format(arguments),
                            ),
                        )
                        if detail is not None
                    ),
                )
            )
        if usage is not None:
            facts.append(self._usage_fact(usage, index, len(facts), turn_id, timestamp))
        return ParsedRecord(events=tuple(events), trajectory=tuple(facts), trajectory_events=())

    def _tool_result_record(
        self,
        record: dict,
        message: dict,
        index: int,
        entry_id: str | None,
        timestamp: float | None,
        clip_text: bool,
    ) -> ParsedRecord:
        del record
        turn_id = self._active_turn_id
        raw = _content_text(message.get("content"))
        name = message.get("toolName") if isinstance(message.get("toolName"), str) else None
        call_id = message.get("toolCallId") if isinstance(message.get("toolCallId"), str) else None
        failed = message.get("isError") is True
        status = TrajectoryStatus.ERROR if failed else TrajectoryStatus.COMPLETED
        usage = _usage(message, entry_id, index)
        event = Event(
            kind=EventKind.TOOL_RESULT,
            text=clipper(clip_text)(raw),
            raw_text=raw,
            tool_name=name,
            ts=timestamp,
            turn_id=turn_id,
            raw_index=index,
            usage=usage,
        )
        fact = _pi_fact(
            kind=TrajectoryKind.TOOL_RESULT,
            summary=raw or name or "",
            native_id=f"{call_id}:result" if call_id else entry_id,
            raw_index=index,
            turn_id=turn_id,
            call_id=call_id,
            status=status,
            timing=_timing(timestamp),
            failure=tool_failure(status, raw or "Pi tool failed"),
            details=tuple(
                detail
                for detail in (
                    optional_trajectory_detail("tool", name),
                    optional_trajectory_detail(
                        "result", message.get("content"), format=ContentFormat.TEXT
                    ),
                )
                if detail is not None
            ),
        )
        facts = [fact]
        if usage is not None:
            facts.append(self._usage_fact(usage, index, 1, turn_id, timestamp))
        return ParsedRecord(events=(event,), trajectory=tuple(facts), trajectory_events=())

    def _summary_record(
        self,
        record: dict,
        index: int,
        entry_id: str | None,
        timestamp: float | None,
    ) -> ParsedRecord:
        summary = record.get("summary") if isinstance(record.get("summary"), str) else ""
        usage = _usage(record, entry_id, index)
        if usage is not None:
            usage = replace(
                usage,
                model=usage.model or self._last_model,
                provider=usage.provider or self._last_provider,
            )
        fact = _pi_fact(
            kind=TrajectoryKind.CONTEXT,
            summary=summary or str(record.get("type")),
            native_id=entry_id,
            raw_index=index,
            timing=_timing(timestamp),
            details=tuple(
                detail
                for detail in (
                    optional_trajectory_detail("tokens_before", record.get("tokensBefore")),
                    optional_trajectory_detail("from_id", record.get("fromId")),
                )
                if detail is not None
            ),
        )
        facts = [fact]
        if usage is not None:
            facts.append(self._usage_fact(usage, index, 1, self._active_turn_id, timestamp))
        return ParsedRecord(
            events=(
                Event(
                    kind=EventKind.ASSISTANT,
                    ts=timestamp,
                    raw_index=index,
                    usage=usage,
                ),
            )
            if usage is not None
            else (),
            trajectory=tuple(facts),
            trajectory_events=(),
        )

    @staticmethod
    def _usage_fact(
        usage: TokenUsage,
        index: int,
        ordinal: int,
        turn_id: str | None,
        timestamp: float | None,
    ) -> TrajectoryFact:
        return _pi_fact(
            kind=TrajectoryKind.USAGE,
            summary=usage.model or "model usage",
            native_id=f"{usage.idempotency_key}:usage" if usage.idempotency_key else None,
            raw_index=index,
            event_ordinal=ordinal,
            turn_id=turn_id,
            timing=_timing(timestamp),
            usage=trajectory_usage_from_token_usage(usage),
        )
