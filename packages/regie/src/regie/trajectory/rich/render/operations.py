"""Call/result associations for display, preserving canonical accounting domains."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from hashlib import sha256

from theater.frontend.trajectory import (
    TrajectoryKind,
    TrajectoryRecord,
    TrajectoryToolOperation,
    tool_operations_for_records,
)

_THEATER_KINDS = {
    TrajectoryKind.THEATER_CALL: TrajectoryKind.TOOL_CALL,
    TrajectoryKind.THEATER_RESULT: TrajectoryKind.TOOL_RESULT,
}


def display_operations_for_records(
    records: Sequence[TrajectoryRecord],
) -> tuple[TrajectoryToolOperation, ...]:
    """Reuse exact pairing on separate copies so Theater never joins an ordinary tool."""
    theater_records = tuple(
        replace(record, kind=_THEATER_KINDS[record.kind])
        for record in records
        if record.kind in _THEATER_KINDS
    )
    theater_operations = tuple(
        replace(
            operation,
            operation_id=f"theater:{sha256(operation.operation_id.encode()).hexdigest()}",
        )
        for operation in tool_operations_for_records(theater_records)
    )
    return (*tool_operations_for_records(records), *theater_operations)
