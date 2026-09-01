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
# stopReason values that end a response. ``stop`` is a genuine finish with
# no tool calls: the turn closes immediately, as it always has. ``toolUse``
# keeps the turn open (tools will run). ``error``/``length`` are retryable by
# Pi's agent-core (auto-retry or compact-and-retry) and ``aborted`` is a user
# cancellation -- none of these three may close a Theater turn from the
# assistant record alone; see the lifecycle marker handling below.
_IMMEDIATE_CLOSE_STOPS = {"stop"}
_DEFERRED_CLOSE_STOPS = {"error", "length", "aborted"}
_LIFECYCLE_CUSTOM_TYPE = "theater:lifecycle"
_LIFECYCLE_VERSION = 1
_LIFECYCLE_PHASES = frozenset({"retry-scheduled", "compaction-will-retry", "settled"})


def _assistant_status(stop_reason: object) -> TrajectoryStatus:
    """Classify one durable Pi assistant message, independently of turn completion."""
    if stop_reason == "error":
        return TrajectoryStatus.ERROR
    if stop_reason == "aborted":
        return TrajectoryStatus.INTERRUPTED
    if stop_reason in {"stop", "length", "toolUse"}:
        return TrajectoryStatus.COMPLETED
    return TrajectoryStatus.UNKNOWN


def _tool_call_status(stop_reason: object, deferred_close: bool) -> TrajectoryStatus:
    """Status for toolCall blocks in an assistant response.

    A normal ``stop`` or ``toolUse`` leaves calls PENDING (they will execute or
    were a normal request). An ``error``/``length``/``aborted`` response is
    discarded by Pi's agent-core before any toolCall blocks execute, so those
    calls must be terminal: INTERRUPTED for a user-cancelled ``aborted``, ERROR
    for a retryable ``error``/``length``, never PENDING.
    """
    if not deferred_close:
        return TrajectoryStatus.PENDING
    return TrajectoryStatus.INTERRUPTED if stop_reason == "aborted" else TrajectoryStatus.ERROR


def _pi_mcp_identity(value: object) -> tuple[str, str] | None:
    """Decode the ``server__tool`` names emitted by Pi MCP extensions."""
    if not isinstance(value, str):
        return None
    server, separator, tool = value.partition("__")
    if not separator:
        return None
    server_id = trajectory_identifier(server)
    tool_id = trajectory_identifier(tool)
    return (server_id, tool_id) if server_id is not None and tool_id is not None else None


def _record_id(record: dict) -> str | None:
    return trajectory_identifier(record.get("id"), overflow_prefix="pi")


def _lifecycle_phase(record: dict) -> str | None:
    """Decode a durable Theater lifecycle custom entry, or return ``None``.

    Pi's SessionManager persists custom entries as ``{"type":"custom",
    "customType":<str>, "data":<obj>, ...}`` JSONL records that do not
    participate in LLM context. Theater's bundled extension writes
    ``theater:lifecycle`` markers so the parser can tell a final error/length
    assistant response (the agent-core will retry or compact-and-retry) from
    a genuinely settled one. Unknown/malformed markers return ``None`` and
    never raise.
    """
    if record.get("type") != "custom":
        return None
    if record.get("customType") != _LIFECYCLE_CUSTOM_TYPE:
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    if data.get("version") != _LIFECYCLE_VERSION:
        return None
    phase = data.get("phase")
    return phase if isinstance(phase, str) and phase in _LIFECYCLE_PHASES else None


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


def _thinking_text(block: dict) -> str:
    """Extract reasoning from a Pi ThinkingContent block.

    Pi's thinking block carries its content in the ``thinking`` field, not
    ``text`` (which belongs to TextContent). The field may be absent or empty
    when the provider redacts reasoning, so we never fabricate content.
    """
    value = block.get("thinking")
    return value if isinstance(value, str) else ""


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
    # Pi uses zero as a placeholder when its provider/model configuration has
    # no price metadata.  Treating that as a reported price prevents Theater's
    # catalog estimator from running for otherwise billable token usage.
    cost, provenance = reported_cost(total_cost, strict_positive=True)
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


def _record_timestamp(record: dict) -> float | None:
    """The outer ``record.timestamp`` — when Pi persisted the entry on ``message_end``.

    Pi writes two timestamps per message: an inner ``message.timestamp`` set by
    pi-ai when the pending response object is created (≈ generation start) and
    an outer ``record.timestamp`` set by the session manager on ``message_end``
    persistence (≈ completion). Both are source-native but Pi does not document
    them as start/end, so callers that pair them mark the interval ``DERIVED``.
    """
    return iso_epoch(record.get("timestamp"))


def _message_timestamp(message: dict | None) -> float | None:
    """The inner ``message.timestamp`` — set by pi-ai at pending-response creation.

    Unlike ``_timestamp``, this does NOT fall back to the outer ``record.timestamp``;
    it returns ``None`` when the inner timestamp is absent so the caller can tell the
    two apart and build the correct interval (outer-only must become
    ``Timing(end=outer, SOURCE)``, not a zero-width ``start=end=outer`` interval).
    """
    if not isinstance(message, dict):
        return None
    value = message.get("timestamp")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) / 1_000
    return None


def _interval_timing(start: float | None, end: float | None) -> Timing | None:
    """Build a ``DERIVED`` interval from Pi's inner (start) and outer (end) timestamps.

    Falls back to a single-endpoint ``SOURCE`` timing when only one timestamp is
    present, so partial or malformed records keep the existing point semantics
    instead of fabricating an interval. The inner timestamp is treated as the
    start (not the end) so we never repeat the original mislabeling bug.
    """
    if start is None and end is None:
        return None
    if start is not None and end is not None and end >= start:
        return Timing(
            start=start,
            end=end,
            duration_ms=(end - start) * 1_000,
            provenance=TimingProvenance.DERIVED,
        )
    if start is not None:
        return Timing(start=start, provenance=TimingProvenance.SOURCE)
    assert end is not None
    return Timing(end=end, provenance=TimingProvenance.SOURCE)


def _event_timestamp(inner: float | None, outer: float | None) -> float | None:
    """Control Event.ts for assistant records: prefer outer completion.

    Pi writes two timestamps per assistant message: the inner
    ``message.timestamp`` (generation start) and the outer ``record.timestamp``
    (completion/persistence). Bus and trajectory events should stamp the moment
    the record was finalized, so the outer timestamp wins when present and the
    inner is the fallback. This keeps Event.ts independent of the Trajectory
    Timing interval, which is built separately from both timestamps.
    """
    if outer is not None:
        return outer
    return inner


def _tool_call_timing(assistant_outer: float | None) -> Timing | None:
    """Anchor a tool call at the containing assistant message's completion time.

    The tool call lives in the same assistant record as the ``toolCall`` block,
    so the assistant's outer (completion) timestamp is the closest durable
    anchor for "the model emitted this call." Marked ``DERIVED`` because the
    interval semantics (call starts when the assistant completes) are inferred
    by Theater, not reported by Pi.
    """
    if assistant_outer is None:
        return None
    return Timing(start=assistant_outer, provenance=TimingProvenance.DERIVED)


class PiParserMixin:
    _active_turn_id: str | None
    _last_model: str | None
    _last_provider: str | None
    #: A deferred terminal assistant response (error/length/aborted) whose turn
    #: cannot close until a durable ``theater:lifecycle`` marker confirms Pi
    #: has settled. Carried across ``parse_record`` calls and drain batches.
    #: Presence is tracked separately from the optional turn id: a deferred
    #: assistant record may have no entry id, and ``turn_id is None`` must not
    #: be read as "no pending terminal".
    _pending_terminal: bool
    _pending_terminal_turn_id: str | None

    def _reset_turn_context(self) -> None:
        self._active_turn_id = None
        self._last_model = None
        self._last_provider = None
        self._pending_terminal = False
        self._pending_terminal_turn_id = None

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
                        status=TrajectoryStatus.COMPLETED,
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
                        status=TrajectoryStatus.COMPLETED,
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
        phase = _lifecycle_phase(record)
        if phase is not None:
            return self._lifecycle_record(phase, index, entry_id, timestamp)
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
        phase = _lifecycle_phase(record)
        if phase is not None:
            # Reconstruct lifecycle state from bounded history so a restart or
            # reattach cannot carry a stale deferred terminal across the gap.
            # retry-scheduled/compaction-will-retry are informational: they signal
            # continued work, not its success. Compaction may still fail and then
            # agent_settled must close, so they must not clear the pending
            # terminal candidate.
            if phase == "settled":
                self._pending_terminal = False
                self._pending_terminal_turn_id = None
                self._active_turn_id = None
            return
        if record_type != "message" or not isinstance((message := record.get("message")), dict):
            return
        role = message.get("role")
        if role == "user":
            self._active_turn_id = _record_id(record)
            self._pending_terminal = False
            self._pending_terminal_turn_id = None
        elif role == "assistant":
            stop_reason = message.get("stopReason")
            calls = _blocks(message.get("content"), "toolCall")
            if stop_reason in _IMMEDIATE_CLOSE_STOPS and not calls:
                # A normal stop closes the turn and clears any deferred terminal.
                self._active_turn_id = None
                self._pending_terminal = False
                self._pending_terminal_turn_id = None
            elif stop_reason in _DEFERRED_CLOSE_STOPS:
                # Retain the deferred terminal candidate tied to the active turn.
                self._pending_terminal = True
                self._pending_terminal_turn_id = self._active_turn_id
            # toolUse and unknown stops leave the active turn open; a later
            # assistant record replaces the deferred candidate only via the
            # explicit close/defer branches above.
            self._last_model = (
                message.get("model") if isinstance(message.get("model"), str) else None
            )
            self._last_provider = (
                message.get("provider") if isinstance(message.get("provider"), str) else None
            )

    def _lifecycle_record(
        self,
        phase: str,
        index: int,
        entry_id: str | None,
        timestamp: float | None,
    ) -> ParsedRecord:
        """Resolve a deferred terminal turn on a durable lifecycle marker.

        ``retry-scheduled`` and ``compaction-will-retry`` are informational
        no-ops: they signal continued work, not its success. Compaction may
        fail and then ``agent_settled`` must still close the turn, so clearing
        the pending terminal here would lose the final ``turn_end``. They are
        accepted (validated phase) but do not change lifecycle state.

        ``settled`` releases a retained deferred terminal as exactly one
        synthetic ``turn_end`` control event with no duplicated assistant text,
        usage, or trajectory facts, then clears the active turn. A settled
        marker with no pending terminal is a no-op (a normal stop already
        closed the turn). The pending turn id is optional -- a deferred
        assistant record with no entry id closes with ``turn_id=None``.
        """
        if phase in {"retry-scheduled", "compaction-will-retry"}:
            return ParsedRecord()
        if phase != "settled":
            return ParsedRecord()
        if not self._pending_terminal:
            # A normal stop already closed the turn; the marker is redundant.
            return ParsedRecord()
        turn_id = self._pending_terminal_turn_id
        self._pending_terminal = False
        self._pending_terminal_turn_id = None
        self._active_turn_id = None
        return ParsedRecord(
            events=(
                Event(
                    kind=EventKind.ASSISTANT,
                    ts=timestamp,
                    turn_end=True,
                    turn_id=turn_id,
                    raw_index=index,
                ),
            ),
            trajectory=(),
            trajectory_events=(),
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
                status=TrajectoryStatus.COMPLETED,
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
            inner = _message_timestamp(message)
            outer = _record_timestamp(record)
            return self._assistant_record(record, message, index, entry_id, inner, outer, clip_text)
        if role == "toolResult":
            return self._tool_result_record(record, message, index, entry_id, timestamp, clip_text)
        return ParsedRecord()

    def _assistant_record(
        self,
        record: dict,
        message: dict,
        index: int,
        entry_id: str | None,
        inner: float | None,
        outer: float | None,
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
        assistant_status = _assistant_status(stop_reason)
        # Event.ts prefers the outer (completion) timestamp so the bus stamp is the
        # moment Pi finalized the record, independent of the Trajectory interval.
        event_ts = _event_timestamp(inner, outer)
        # Determine the turn-close policy from the durable stop reason. A normal
        # ``stop`` with no tool calls closes the turn immediately, as it always
        # has. ``error``/``length``/``aborted`` are retryable or cancellable and
        # must not close from the assistant record alone: the parser retains a
        # pending terminal candidate and only a durable ``theater:lifecycle"
        # settled marker releases it. ``toolUse`` and unknown stops keep the
        # turn open (tools will run).
        immediate_close = stop_reason in _IMMEDIATE_CLOSE_STOPS and not calls
        deferred_close = stop_reason in _DEFERRED_CLOSE_STOPS
        # error/length/aborted responses may carry partial toolCall blocks that
        # the agent-core never executes (it discards the response and retries,
        # or cancels). Those calls must be terminal, never PENDING.
        call_status = _tool_call_status(stop_reason, deferred_close)
        events: list[Event] = []
        if raw:
            events.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    text=clipper(clip_text)(raw),
                    raw_text=raw,
                    ts=event_ts,
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
                    ts=event_ts,
                    turn_id=turn_id,
                    raw_index=index,
                )
            )
        if usage is not None:
            events.append(
                Event(
                    kind=EventKind.ASSISTANT,
                    ts=event_ts,
                    turn_id=turn_id,
                    raw_index=index,
                    usage=usage,
                )
            )
        error = message.get("errorMessage") if isinstance(message.get("errorMessage"), str) else ""
        if deferred_close and error and not raw:
            events.append(
                Event(
                    kind=EventKind.ERROR,
                    text=clipper(clip_text)(error),
                    raw_text=error,
                    ts=event_ts,
                    turn_id=turn_id,
                    raw_index=index,
                )
            )
        if immediate_close:
            if events:
                events[-1] = replace(events[-1], turn_end=True)
            else:
                events.append(
                    Event(
                        kind=EventKind.ASSISTANT,
                        ts=event_ts,
                        turn_end=True,
                        turn_id=turn_id,
                        raw_index=index,
                    )
                )
            self._active_turn_id = None
            self._pending_terminal = False
            self._pending_terminal_turn_id = None
        elif deferred_close:
            # Retain the deferred terminal candidate; a lifecycle marker decides.
            self._pending_terminal = True
            self._pending_terminal_turn_id = turn_id
        # toolUse and unknown stops leave the active turn open.

        facts: list[TrajectoryFact] = [
            _pi_fact(
                kind=TrajectoryKind.ASSISTANT,
                summary=raw or error,
                native_id=entry_id,
                raw_index=index,
                turn_id=turn_id,
                timing=_interval_timing(inner, outer),
                status=assistant_status,
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
            text = _thinking_text(block)
            if text:
                facts.append(
                    _pi_fact(
                        kind=TrajectoryKind.REASONING,
                        summary=text,
                        native_id=f"{entry_id}:thinking:{len(facts)}" if entry_id else None,
                        raw_index=index,
                        event_ordinal=len(facts),
                        turn_id=turn_id,
                        status=assistant_status,
                        timing=_interval_timing(inner, outer),
                    )
                )
        for call in calls:
            call_id = call.get("id") if isinstance(call.get("id"), str) else None
            name = call.get("name") if isinstance(call.get("name"), str) else None
            mcp_identity = _pi_mcp_identity(name)
            mcp_server, mcp_tool = mcp_identity or (None, None)
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
                    mcp_server=mcp_server,
                    mcp_tool=mcp_tool,
                    status=call_status,
                    timing=_tool_call_timing(outer),
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
            facts.append(
                self._usage_fact(usage, index, len(facts), turn_id, inner, outer, interval=True)
            )
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
        mcp_identity = _pi_mcp_identity(name)
        mcp_server, mcp_tool = mcp_identity or (None, None)
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
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
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
            status=TrajectoryStatus.COMPLETED,
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
        outer: float | None = None,
        *,
        interval: bool = False,
    ) -> TrajectoryFact:
        return _pi_fact(
            kind=TrajectoryKind.USAGE,
            summary=usage.model or "model usage",
            status=TrajectoryStatus.COMPLETED,
            native_id=f"{usage.idempotency_key}:usage" if usage.idempotency_key else None,
            raw_index=index,
            event_ordinal=ordinal,
            turn_id=turn_id,
            timing=_interval_timing(timestamp, outer) if interval else _timing(timestamp),
            usage=trajectory_usage_from_token_usage(usage),
        )
