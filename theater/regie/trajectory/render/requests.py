"""Cached request projection and plain ledger header text."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from theater.regie.trajectory.render.records import (
    compact_cost,
    compact_number,
    format_duration,
    sanitize_text,
    status_label,
)
from theater.trajectory import TrajectoryRecord, TrajectoryRequest, requests_for_records


@dataclass(frozen=True, slots=True)
class RequestIndex:
    """Immutable request membership for one bounded record window."""

    ordered: tuple[TrajectoryRequest, ...] = ()
    by_id: Mapping[str, TrajectoryRequest] = MappingProxyType({})
    by_record_id: Mapping[str, str] = MappingProxyType({})


def empty_request_index() -> RequestIndex:
    """Return the shared-shape empty request index."""
    return RequestIndex()


def build_request_index(records: Iterable[TrajectoryRecord]) -> RequestIndex:
    """Project records once and index each projected member by request ID."""
    ordered = requests_for_records(records)
    by_id: dict[str, TrajectoryRequest] = {}
    by_record_id: dict[str, str] = {}
    for request in ordered:
        prior_request = by_id.setdefault(request.request_id, request)
        if prior_request != request:
            raise ValueError("trajectory request projection repeated a canonical request ID")
        for record_id in request.record_ids:
            prior = by_record_id.setdefault(record_id, request.request_id)
            if prior != request.request_id:
                raise ValueError(
                    "trajectory request projection joined a record to multiple requests"
                )
    return RequestIndex(
        ordered=ordered,
        by_id=MappingProxyType(by_id),
        by_record_id=MappingProxyType(by_record_id),
    )


@dataclass(frozen=True, slots=True)
class RequestRowText:
    """Plain, bounded presentation values for one request header."""

    event: str
    source: str
    summary: str
    status: str
    duration: str


def _one_line(value: str) -> str:
    return sanitize_text(value).replace("\r", " ").replace("\n", " ").replace("\t", " ")


def request_row_text(request: TrajectoryRequest, *, compact: bool = False) -> RequestRowText:
    """Build one non-interactive request header without Rich or Textual values."""
    model = _one_line(request.model) if request.model else "model unknown"
    provider = _one_line(request.provider) if request.provider else ""
    source = model
    if provider and not model.startswith(f"{provider}/"):
        source = f"{provider}/{model}"
    if request.usage is None:
        summary = "usage unavailable"
    else:
        usage = request.usage
        cost = (
            "—"
            if usage.cost_usd is None
            else f"${compact_cost(usage.cost_usd)} {usage.cost_provenance.value}"
        )
        summary = " · ".join(
            (
                f"in {compact_number(usage.input_tokens)}",
                f"out {compact_number(usage.output_tokens)}",
                f"cache {compact_number(usage.cache_read_tokens + usage.cache_write_tokens)}",
                f"reasoning {compact_number(usage.reasoning_tokens)}",
                f"cost {cost}",
            )
        )
    if request.records_truncated:
        summary += " · links clipped"
    if request.failure is not None:
        summary += f" · failure {request.failure.category.value.replace('_', ' ')}"
    if request.retry_of_record_id is not None:
        attempt = f" {request.retry_attempt}" if request.retry_attempt is not None else ""
        summary += f" · retry{attempt}"
    if compact:
        summary = f"[{source}] {summary}"
    return RequestRowText(
        event="◆ REQUEST",
        source=source,
        summary=summary,
        status=status_label(request.status),
        duration=format_duration(request.timing),
    )


__all__ = [
    "RequestIndex",
    "RequestRowText",
    "build_request_index",
    "empty_request_index",
    "request_row_text",
]
