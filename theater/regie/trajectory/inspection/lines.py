"""Bounded presentation values for request, failure, retry, and tool inspectors."""

from __future__ import annotations

from dataclasses import dataclass

from theater.trajectory import TrajectoryRequest, TrajectoryToolOperation
from theater.trajectory.records import TrajectoryFailure


@dataclass(frozen=True, slots=True)
class InspectorLine:
    text: str
    target_record_id: str | None = None


def failure_lines(failure: TrajectoryFailure | None) -> tuple[InspectorLine, ...]:
    if failure is None:
        return ()
    lines = [InspectorLine(f"Failure: {failure.category.value.replace('_', ' ')}")]
    if failure.code:
        lines.append(InspectorLine(f"Code: {failure.code}"))
    if failure.detail:
        detail = failure.detail.splitlines()
        lines.append(InspectorLine(f"Detail: {detail[0]}"))
        lines.extend(InspectorLine(value) for value in detail[1:])
    return tuple(lines)


def retry_lines(
    retry_of_record_id: str | None,
    retry_attempt: int | None,
) -> tuple[InspectorLine, ...]:
    if retry_of_record_id is None:
        return ()
    suffix = f" · attempt {retry_attempt}" if retry_attempt is not None else ""
    return (InspectorLine(f"Retry of: {retry_of_record_id}{suffix}", retry_of_record_id),)


def request_summary_lines(request: TrajectoryRequest | None) -> tuple[InspectorLine, ...]:
    if request is None:
        return ()
    lines = [
        InspectorLine(f"Request: {request.source_request_id or request.request_id}"),
        InspectorLine(f"Status: {request.status.value.replace('_', ' ')}"),
    ]
    if request.provider:
        lines.append(InspectorLine(f"Provider: {request.provider}"))
    if request.model:
        lines.append(InspectorLine(f"Model: {request.model}"))
    lines.extend(failure_lines(request.failure))
    lines.extend(retry_lines(request.retry_of_record_id, request.retry_attempt))
    if request.records_truncated:
        lines.append(InspectorLine("Associations: retained links clipped"))
    return tuple(lines)


def request_usage_lines(request: TrajectoryRequest | None) -> tuple[InspectorLine, ...]:
    usage = request.usage if request is not None else None
    if usage is None:
        return (InspectorLine("No usage recorded."),)
    lines: list[InspectorLine] = []
    if usage.provider:
        lines.append(InspectorLine(f"Provider: {usage.provider}"))
    if usage.model:
        lines.append(InspectorLine(f"Model: {usage.model}"))
    lines.extend(
        (
            InspectorLine(f"Input tokens: {usage.input_tokens}"),
            InspectorLine(f"Output tokens: {usage.output_tokens}"),
            InspectorLine(f"Reasoning tokens: {usage.reasoning_tokens}"),
            InspectorLine(f"Cache read tokens: {usage.cache_read_tokens}"),
            InspectorLine(f"Cache write tokens: {usage.cache_write_tokens}"),
        )
    )
    if usage.cost_usd is None:
        lines.append(InspectorLine("Cost: unavailable"))
    else:
        lines.append(InspectorLine(f"Cost: ${usage.cost_usd:g} · {usage.cost_provenance.value}"))
    return tuple(lines)


def request_timing_lines(request: TrajectoryRequest | None) -> tuple[InspectorLine, ...]:
    timing = request.timing if request is not None else None
    if timing is None:
        return (InspectorLine("No timing supplied."),)
    lines = [
        InspectorLine(f"Duration: {_milliseconds(timing.duration_ms)}"),
        InspectorLine(f"Start: {_number(timing.start)}"),
        InspectorLine(f"First token: {_number(timing.first_token)}"),
        InspectorLine(f"End: {_number(timing.end)}"),
        InspectorLine(f"Provenance: {timing.provenance.value}"),
    ]
    if request is not None and request.ttft_ms is not None:
        lines.append(InspectorLine(f"Time to first token: {_milliseconds(request.ttft_ms)}"))
    if request is not None and request.generation_duration_ms is not None:
        lines.append(
            InspectorLine(f"Generation duration: {_milliseconds(request.generation_duration_ms)}")
        )
    if request is not None and request.output_tokens_per_second is not None:
        lines.append(
            InspectorLine(f"Output throughput: {request.output_tokens_per_second:.2f} tok/s")
        )
    return tuple(lines)


def request_association_lines(request: TrajectoryRequest | None) -> tuple[InspectorLine, ...]:
    if request is None:
        return (InspectorLine("No request associations recorded."),)
    groups = (
        ("Context", request.context_record_ids),
        ("Model", request.model_record_ids),
        ("Tools", request.tool_record_ids),
        ("Coordination", request.coordination_record_ids),
    )
    lines: list[InspectorLine] = []
    for label, record_ids in groups:
        if not record_ids:
            continue
        lines.append(InspectorLine(f"{label}: {len(record_ids)}"))
        lines.extend(InspectorLine(f"  {record_id}", record_id) for record_id in record_ids)
    lines.extend(retry_lines(request.retry_of_record_id, request.retry_attempt))
    return tuple(lines or [InspectorLine("No request associations recorded.")])


def tool_summary_lines(tool: TrajectoryToolOperation) -> tuple[InspectorLine, ...]:
    fields = (
        ("Tool", tool.tool_name),
        ("Source", tool.source),
        ("Status", tool.status.value),
        ("Identity", tool.identity.value),
        ("Call ID", tool.call_id),
        ("Request", tool.request_id),
        ("Parent", tool.parent_call_id),
        ("Children", ", ".join(tool.child_call_ids) or None),
        ("Calls", str(tool.call_count)),
        ("Results", str(tool.result_count)),
        ("Records", "links clipped" if tool.records_truncated else None),
    )
    lines = [InspectorLine(f"{name}: {value}") for name, value in fields if value]
    lines.extend(failure_lines(tool.failure))
    lines.extend(retry_lines(tool.retry_of_record_id, tool.retry_attempt))
    return tuple(lines)


def _milliseconds(value: float | None) -> str:
    if value is None:
        return "unavailable"
    if value < 1_000:
        return f"{value:g}ms"
    seconds = f"{value / 1_000:.2f}".rstrip("0").rstrip(".")
    return f"{seconds}s"


def _number(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:g}"


__all__ = [
    "InspectorLine",
    "failure_lines",
    "request_association_lines",
    "request_summary_lines",
    "request_timing_lines",
    "request_usage_lines",
    "retry_lines",
    "tool_summary_lines",
]
