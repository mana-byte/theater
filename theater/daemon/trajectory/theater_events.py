"""Allowlisted Theater-bus projection for participant trajectory streams."""

from __future__ import annotations

import math
from collections.abc import Mapping

from theater.constants.daemon import (
    BUS_KIND_AGENT_OBSERVATION_ERROR,
    BUS_KIND_AGENT_TRANSCRIPT_RECEIPT,
    BUS_KIND_JOB_AWAIT_END,
    BUS_KIND_JOB_AWAIT_START,
    BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
    BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
    BUS_KIND_PARTICIPANT_SESSION_BOUNDARY,
)
from theater.trajectory import (
    ContentFormat,
    DetailField,
    LinkDirection,
    ParticipantLink,
    Timing,
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
)

BUS_KIND_PARTICIPANT_CREATED = "participant.created"
BUS_KIND_PARTICIPANT_DEAD = "participant.dead"
BUS_KIND_AGENT_SEND = "agent.send"
BUS_KIND_AGENT_RECEIVE = "agent.receive"
BUS_KIND_JOB_FINISHED = "job.finished"

BUS_KIND_CATALOG = {
    BUS_KIND_PARTICIPANT_CREATED,
    BUS_KIND_PARTICIPANT_SESSION_BOUNDARY,
    "participant.resumed",
    BUS_KIND_AGENT_SEND,
    BUS_KIND_AGENT_RECEIVE,
    BUS_KIND_JOB_AWAIT_START,
    BUS_KIND_JOB_AWAIT_END,
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
    BUS_KIND_JOB_FINISHED,
    "job.failure",
    BUS_KIND_AGENT_TRANSCRIPT_RECEIPT,
    BUS_KIND_OPERATOR_TRANSCRIPT_BIND,
    BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND,
    BUS_KIND_AGENT_OBSERVATION_ERROR,
    BUS_KIND_PARTICIPANT_DEAD,
}
ALLOWLISTED_BUS_KINDS = frozenset(BUS_KIND_CATALOG)


def project_bus_row(row: Mapping[str, object], participant_id: str) -> TrajectoryRecord | None:
    """Project one coordination row only when it affects this participant."""
    row_id = row.get("id")
    kind = row.get("kind")
    if (
        type(row_id) is not int
        or row_id < 0
        or not isinstance(kind, str)
        or kind not in ALLOWLISTED_BUS_KINDS
    ):
        return None
    from_id = _string_or_none(row.get("from_id"))
    to_id = _string_or_none(row.get("to_id"))
    if participant_id not in {from_id, to_id}:
        return None
    payload = row.get("payload")
    payload_map = payload if isinstance(payload, Mapping) else {}
    if kind == BUS_KIND_JOB_FINISHED and str(payload_map.get("state", "")) not in {
        "crashed",
        "killed",
        "error",
        "failed",
    }:
        return None
    record_kind = _record_kind(kind, participant_id, from_id, to_id, payload_map)
    status = _record_status(kind, payload_map)
    summary = _summary(kind, payload_map, record_kind)
    details = _details(payload_map, summary)
    links = _links(participant_id, from_id, to_id)
    timing = _timing(row.get("ts"), payload_map)
    return TrajectoryRecord(
        record_id=f"bus:{row_id}",
        revision=0,
        participant_id=participant_id,
        source_epoch="theater-bus",
        lane=TrajectoryLane.THEATER,
        kind=record_kind,
        source="theater",
        summary=summary,
        status=status,
        raw_index=row_id,
        source_offset=row_id,
        links=links,
        timing=timing,
        details=details,
    )


def _record_kind(
    bus_kind: str,
    participant_id: str,
    from_id: str | None,
    to_id: str | None,
    payload: Mapping[str, object],
) -> TrajectoryKind:
    if bus_kind == BUS_KIND_AGENT_SEND:
        return TrajectoryKind.SEND if participant_id == from_id else TrajectoryKind.RECEIVE
    if bus_kind == BUS_KIND_AGENT_RECEIVE:
        return TrajectoryKind.RECEIVE
    if bus_kind == BUS_KIND_PARTICIPANT_CREATED:
        return TrajectoryKind.SPAWN
    if bus_kind in {BUS_KIND_PARTICIPANT_SESSION_BOUNDARY, "participant.resumed"}:
        return (
            TrajectoryKind.RESUME
            if participant_id == to_id or payload.get("reason") == "resume"
            else TrajectoryKind.SESSION_BOUNDARY
        )
    if bus_kind == BUS_KIND_JOB_AWAIT_START:
        return TrajectoryKind.AWAIT_START
    if bus_kind == BUS_KIND_JOB_AWAIT_END:
        return TrajectoryKind.AWAIT_END
    if bus_kind == BUS_KIND_PARTICIPANT_KILL_REQUESTED:
        return TrajectoryKind.KILL
    if bus_kind in {BUS_KIND_JOB_FINISHED, "job.failure"}:
        return TrajectoryKind.JOB_FAILURE
    if bus_kind == BUS_KIND_AGENT_OBSERVATION_ERROR:
        return TrajectoryKind.OBSERVATION_ERROR
    if bus_kind == BUS_KIND_PARTICIPANT_DEAD:
        return TrajectoryKind.THEATER
    return TrajectoryKind.TRANSCRIPT_BOUNDARY


def _record_status(bus_kind: str, payload: Mapping[str, object]) -> TrajectoryStatus:
    if bus_kind == BUS_KIND_JOB_AWAIT_START:
        return TrajectoryStatus.RUNNING
    if bus_kind == BUS_KIND_JOB_AWAIT_END:
        return {
            "completed": TrajectoryStatus.COMPLETED,
            "timeout": TrajectoryStatus.TIMEOUT,
            "cancelled": TrajectoryStatus.CANCELLED,
            "error": TrajectoryStatus.ERROR,
        }.get(str(payload.get("state")), TrajectoryStatus.UNKNOWN)
    if bus_kind in {BUS_KIND_AGENT_OBSERVATION_ERROR, BUS_KIND_PARTICIPANT_DEAD}:
        return TrajectoryStatus.ERROR
    if bus_kind in {BUS_KIND_JOB_FINISHED, "job.failure"}:
        return TrajectoryStatus.ERROR
    return TrajectoryStatus.COMPLETED


def _summary(bus_kind: str, payload: Mapping[str, object], record_kind: TrajectoryKind) -> str:
    handle = _text(payload.get("handle"))
    state = _text(payload.get("state"))
    if bus_kind == BUS_KIND_JOB_AWAIT_START:
        return f"Await {handle or 'job'} started"
    if bus_kind == BUS_KIND_JOB_AWAIT_END:
        return f"Await {handle or 'job'} {state or 'ended'}"
    if bus_kind in {BUS_KIND_JOB_FINISHED, "job.failure"}:
        return f"Job {handle or 'job'} failed"
    dynamic = {
        BUS_KIND_AGENT_SEND: _text(payload.get("prompt")) or "Prompt sent",
        BUS_KIND_AGENT_RECEIVE: _text(payload.get("text")) or "Message received",
        BUS_KIND_AGENT_OBSERVATION_ERROR: (
            _text(payload.get("message")) or _text(payload.get("code")) or "Observation error"
        ),
    }
    fixed = {
        BUS_KIND_AGENT_TRANSCRIPT_RECEIPT: "Transcript identity received",
        BUS_KIND_OPERATOR_TRANSCRIPT_BIND: "Transcript binding changed",
        BUS_KIND_OPERATOR_TRANSCRIPT_UNBIND: "Transcript binding changed",
        BUS_KIND_PARTICIPANT_KILL_REQUESTED: "Participant kill requested",
        BUS_KIND_PARTICIPANT_CREATED: "Participant spawned",
        BUS_KIND_PARTICIPANT_DEAD: "Participant became dead",
    }
    if bus_kind in {BUS_KIND_PARTICIPANT_SESSION_BOUNDARY, "participant.resumed"}:
        return "Session resumed" if record_kind is TrajectoryKind.RESUME else "Session boundary"
    return dynamic.get(bus_kind, fixed.get(bus_kind, bus_kind))


def _details(payload: Mapping[str, object], summary: str) -> tuple[DetailField, ...]:
    fields: list[DetailField] = []
    for name in ("handle", "state", "reason", "error_code", "code", "elapsed_seconds"):
        value = payload.get(name)
        text = _text(value)
        if text and text != summary:
            fields.append(DetailField.from_text(name, text, format=ContentFormat.TEXT))
    for name in ("prompt", "text", "message"):
        value = _text(payload.get(name))
        if value and value != summary:
            fields.append(DetailField.from_text(name, value, format=ContentFormat.TEXT))
    return tuple(fields)


def _links(
    participant_id: str, from_id: str | None, to_id: str | None
) -> tuple[ParticipantLink, ...]:
    links: list[ParticipantLink] = []
    if from_id is not None and from_id != participant_id and to_id == participant_id:
        links.append(ParticipantLink(from_id, "sender", LinkDirection.INCOMING))
    if to_id is not None and to_id != participant_id and from_id == participant_id:
        links.append(ParticipantLink(to_id, "recipient", LinkDirection.OUTGOING))
    return tuple(links)


def _timing(timestamp: object, payload: Mapping[str, object]) -> Timing | None:
    end = _finite_number(timestamp)
    if end is None:
        return None
    elapsed = _finite_number(payload.get("elapsed_seconds"))
    duration_ms = elapsed * 1000 if elapsed is not None and elapsed >= 0 else None
    start = end - elapsed if elapsed is not None and elapsed >= 0 else end
    return Timing(
        start=start,
        end=end,
        duration_ms=duration_ms,
        provenance=TimingProvenance.OBSERVED,
    )


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return str(value)
    return ""


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = [
    "ALLOWLISTED_BUS_KINDS",
    "BUS_KIND_CATALOG",
    "project_bus_row",
]
