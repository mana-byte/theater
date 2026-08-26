"""Structured log emission for canonical agent trajectory records."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

from theater.constants.observability import AGENT_LOG_BODY_MAX_BYTES, AGENT_TRAJECTORY_LOG_EVENT
from theater.trajectory import Timing, TimingProvenance, TrajectoryRecord, TrajectoryStatus

from .attributes import Scalar, optional
from .state import AgentTelemetryState, ParticipantEmissionState

_INT64_MIN = -(2**63)
_INT64_MAX = 2**63 - 1


class AgentLogEmitter:
    """Emit one structured event for each newly accepted record revision."""

    def __init__(self, bridge: Any, state: AgentTelemetryState, *, include_content: bool) -> None:
        self._bridge = bridge
        self._state = state
        self._include_content = include_content

    def record(
        self,
        state: ParticipantEmissionState,
        participant: Any,
        harness: str,
        records: tuple[TrajectoryRecord, ...],
        contexts: Mapping[str, Any],
    ) -> None:
        """Emit every newly highest canonical revision without retaining its body."""
        for record in records:
            if not self._state.needs_log(state, record):
                continue
            body, body_attributes = _body(record, include_content=self._include_content)
            attributes = {
                **_attributes(record, participant, harness),
                **body_attributes,
            }
            try:
                emitted = self._bridge.emit_log(
                    AGENT_TRAJECTORY_LOG_EVENT,
                    body=body,
                    attributes=attributes,
                    timestamp_ns=_timestamp_ns(record.timing),
                    severity_text=_severity(record.status),
                    context=contexts.get(record.record_id),
                )
            except Exception:
                continue
            if emitted:
                self._state.remember_log(state, record.record_id, record.revision)


def _attributes(
    record: TrajectoryRecord, participant: Any, harness: str
) -> dict[str, Scalar]:
    attributes: dict[str, Scalar] = {
        "theater.agent.trajectory.schema.version": 1,
        "theater.agent.participant.id": record.participant_id,
        "theater.agent.harness": harness,
        "theater.agent.source.epoch": record.source_epoch,
        "theater.agent.record.id": record.record_id,
        "theater.agent.record.lane": record.lane.value,
        "theater.agent.record.kind": record.kind.value,
        "theater.agent.record.status": record.status.value,
        "theater.agent.record.source": record.source,
    }
    optional(attributes, "theater.agent.record.revision", record.revision)
    optional(attributes, "theater.agent.record.raw_index", record.raw_index)
    optional(attributes, "theater.agent.record.event_ordinal", record.event_ordinal)
    optional(attributes, "theater.agent.record.link_count", len(record.links))
    optional(attributes, "theater.agent.parent.id", getattr(participant, "parent_id", None))
    optional(attributes, "theater.agent.session.id", getattr(participant, "session_id", None))
    optional(attributes, "theater.agent.record.source_offset", record.source_offset)
    optional(attributes, "theater.agent.turn.id", record.turn_id)
    optional(attributes, "theater.agent.step.id", record.step_id)
    optional(attributes, "theater.agent.request.id", record.request_id)
    optional(attributes, "theater.agent.call.id", record.call_id)
    optional(attributes, "theater.agent.parent_call.id", record.parent_call_id)
    optional(attributes, "theater.agent.mcp.server", record.mcp_server)
    optional(attributes, "theater.agent.mcp.tool", record.mcp_tool)
    _timing_attributes(attributes, record.timing)
    _usage_attributes(attributes, record)
    if record.failure is not None:
        attributes["theater.agent.failure.category"] = record.failure.category.value
        optional(attributes, "theater.agent.failure.code", record.failure.code)
    optional(attributes, "theater.agent.retry.of_record_id", record.retry_of_record_id)
    optional(attributes, "theater.agent.retry.attempt", record.retry_attempt)
    return attributes


def _timing_attributes(
    attributes: dict[str, Scalar], timing: Timing | None
) -> None:
    if timing is None:
        return
    attributes["theater.agent.timing.provenance"] = timing.provenance.value
    optional(attributes, "theater.agent.timing.start", timing.start)
    optional(attributes, "theater.agent.timing.end", timing.end)
    optional(attributes, "theater.agent.timing.first_token", timing.first_token)
    optional(attributes, "theater.agent.timing.duration_ms", timing.duration_ms)


def _usage_attributes(
    attributes: dict[str, Scalar], record: TrajectoryRecord
) -> None:
    usage = record.usage
    if usage is None:
        return
    optional(attributes, "theater.agent.model", usage.model)
    optional(attributes, "theater.agent.provider", usage.provider)
    optional(attributes, "theater.agent.usage.request.id", usage.request_id)
    optional(attributes, "theater.agent.usage.input_tokens", usage.input_tokens)
    optional(attributes, "theater.agent.usage.output_tokens", usage.output_tokens)
    optional(attributes, "theater.agent.usage.reasoning_tokens", usage.reasoning_tokens)
    optional(attributes, "theater.agent.usage.cache_read_tokens", usage.cache_read_tokens)
    optional(attributes, "theater.agent.usage.cache_write_tokens", usage.cache_write_tokens)
    optional(attributes, "theater.agent.cost.usd", usage.cost_usd)
    attributes["theater.agent.cost.provenance"] = usage.cost_provenance.value


def _body(record: TrajectoryRecord, *, include_content: bool) -> tuple[str, dict[str, int | bool]]:
    if not include_content:
        return f"{record.kind.value}:{record.status.value}", {
            "theater.agent.log.body.truncated": False,
            "theater.agent.log.body.omitted_bytes": 0,
        }
    body = json.dumps(record.to_wire(), ensure_ascii=False, separators=(",", ":"))
    if len(body.encode("utf-8")) <= AGENT_LOG_BODY_MAX_BYTES:
        return body, {
            "theater.agent.log.body.truncated": False,
            "theater.agent.log.body.omitted_bytes": 0,
        }
    bounded = record.to_wire()
    original_bytes = len(body.encode("utf-8"))
    body = _bounded_json(bounded)
    return body, {
        "theater.agent.log.body.truncated": True,
        "theater.agent.log.body.omitted_bytes": max(0, original_bytes - len(body.encode("utf-8"))),
    }


def _bounded_json(value: dict[str, object]) -> str:
    """Shorten string fields before serializing so the body stays valid JSON."""
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    while len(body.encode("utf-8")) > AGENT_LOG_BODY_MAX_BYTES:
        strings = _string_slots(value)
        candidates = [
            slot for slot in strings if isinstance(slot[0][slot[1]], str) and slot[0][slot[1]]
        ]
        if not candidates:
            if not _trim_collection(value):
                return "{}"
        else:
            parent, key = max(
                candidates,
                key=lambda slot: len(str(slot[0][slot[1]]).encode("utf-8")),
            )
            text = parent[key]
            assert isinstance(text, str)
            parent[key] = _clip(text, max(0, len(text.encode("utf-8")) // 2))
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return body


def _string_slots(value: object) -> list[tuple[Any, Any]]:
    slots: list[tuple[Any, Any]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, str):
                slots.append((value, key))
            else:
                slots.extend(_string_slots(item))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, str):
                slots.append((value, index))
            else:
                slots.extend(_string_slots(item))
    return slots


def _trim_collection(value: object) -> bool:
    """Drop one trailing collection item only after all strings are empty."""
    collections = _collections(value)
    if collections:
        collections[0].pop()
        return True
    return False


def _collections(value: object) -> list[list[object]]:
    result: list[list[object]] = []
    if isinstance(value, dict):
        for item in value.values():
            result.extend(_collections(item))
    elif isinstance(value, list):
        if value:
            result.append(value)
        for item in value:
            result.extend(_collections(item))
    return result


def _clip(value: str, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")


def _severity(status: TrajectoryStatus) -> str:
    if status is TrajectoryStatus.ERROR:
        return "ERROR"
    if status in {
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.INTERRUPTED,
        TrajectoryStatus.CANCELLED,
    }:
        return "WARN"
    return "INFO"


def _timestamp_ns(timing: Timing | None) -> int | None:
    if timing is None or timing.provenance is TimingProvenance.UNAVAILABLE:
        return None
    seconds = timing.end if timing.end is not None else timing.first_token
    seconds = timing.start if seconds is None else seconds
    if seconds is None or not math.isfinite(seconds):
        return None
    try:
        timestamp = seconds * 1_000_000_000
    except OverflowError:
        return None
    if not math.isfinite(timestamp) or not _INT64_MIN <= timestamp <= _INT64_MAX:
        return None
    return int(timestamp)


__all__ = ["AgentLogEmitter"]
