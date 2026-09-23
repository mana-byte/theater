"""Cached request membership for one bounded record window."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from regie.trajectory.domain import TrajectoryRecord, TrajectoryRequest, requests_for_records


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


__all__ = [
    "RequestIndex",
    "build_request_index",
    "empty_request_index",
]
