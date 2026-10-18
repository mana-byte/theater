"""Fake stores and bridges for the agent trajectory signal tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from opentelemetry.trace import (
    NonRecordingSpan,
    SpanContext,
    TraceFlags,
    TraceState,
    set_span_in_context,
)

from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Participant
from theater.trajectory import (
    DetailField,
    Timing,
    TrajectoryFailure,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
    TrajectoryUsage,
)


@dataclass
class SignalStore:
    participant: Participant

    def get_participant(self, participant_id: str) -> Participant | None:
        return self.participant if participant_id == self.participant.id else None


@dataclass
class SignalMetricBridge:
    active: bool = True
    fail: bool = False
    observations: list[object] = field(default_factory=list)

    def observe(self, *args: object) -> None:
        if self.fail:
            raise RuntimeError("metric failure")
        self.observations.append(args)


@dataclass
class SignalBridge:
    active: bool = True
    fail_log: bool = False
    fail_span: bool = False
    log_result: bool = True
    logs: list[dict[str, object]] = field(default_factory=list)
    spans: list[dict[str, object]] = field(default_factory=list)
    _next_span_id: int = 1

    def emit_log(self, event_name: str, **kwargs: object) -> bool:
        if self.fail_log:
            raise RuntimeError("log failure")
        if not self.log_result:
            return False
        self.logs.append({"event_name": event_name, **kwargs})
        return True

    def emit_span(self, name: str, **kwargs: object) -> object | None:
        if self.fail_span:
            raise RuntimeError("span failure")
        span_context = SpanContext(
            trace_id=1,
            span_id=self._next_span_id,
            is_remote=False,
            trace_flags=TraceFlags(1),
            trace_state=TraceState(),
        )
        self._next_span_id += 1
        context = set_span_in_context(NonRecordingSpan(span_context))
        self.spans.append({"name": name, "context": context, **kwargs})
        return context


def signal_participant() -> Participant:
    return Participant(
        id="agent",
        harness="codex",
        parent_id="parent",
        session_id="session",
        transcript_location="one.jsonl",
    )


def signal_fact(
    native_id: str,
    *,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    revision: int = 0,
    timing: Timing | None = None,
    usage: TrajectoryUsage | None = None,
    failure: TrajectoryFailure | None = None,
    request_id: str | None = None,
    call_id: str | None = None,
    parent_call_id: str | None = None,
    details: tuple[DetailField, ...] = (),
    summary: str = "private summary",
    raw_index: int = 1,
) -> TrajectoryFact:
    return TrajectoryFact(
        native_id=native_id,
        kind=kind,
        lane=lane,
        status=status,
        revision=revision,
        timing=timing,
        usage=usage,
        failure=failure,
        request_id=request_id,
        call_id=call_id,
        parent_call_id=parent_call_id,
        details=details,
        summary=summary,
        raw_index=raw_index,
    )
