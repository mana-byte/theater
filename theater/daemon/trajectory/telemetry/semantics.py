"""Shared terminal outcome rules for agent trajectory telemetry."""

from __future__ import annotations

from theater.constants.observability import (
    AGENT_RESULT_CANCELLED,
    AGENT_RESULT_ERROR,
    AGENT_RESULT_INTERRUPTED,
    AGENT_RESULT_SUCCESS,
    AGENT_RESULT_TIMEOUT,
)
from theater.trajectory import (
    TrajectoryFailure,
    TrajectoryStatus,
    TrajectoryToolIdentity,
    TrajectoryToolOperation,
)

TERMINAL_STATUSES = frozenset(
    {
        TrajectoryStatus.COMPLETED,
        TrajectoryStatus.ERROR,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
        TrajectoryStatus.INTERRUPTED,
    }
)
FAILURE_STATUSES = frozenset(
    {
        TrajectoryStatus.ERROR,
        TrajectoryStatus.TIMEOUT,
        TrajectoryStatus.CANCELLED,
        TrajectoryStatus.INTERRUPTED,
    }
)
RESULTS = {
    TrajectoryStatus.COMPLETED: AGENT_RESULT_SUCCESS,
    TrajectoryStatus.ERROR: AGENT_RESULT_ERROR,
    TrajectoryStatus.TIMEOUT: AGENT_RESULT_TIMEOUT,
    TrajectoryStatus.CANCELLED: AGENT_RESULT_CANCELLED,
    TrajectoryStatus.INTERRUPTED: AGENT_RESULT_INTERRUPTED,
}


def final_tool_operation(operation: TrajectoryToolOperation) -> bool:
    """Whether a terminal tool projection cannot later gain its keyed result."""
    return (
        operation.status in TERMINAL_STATUSES
        and operation.identity is not TrajectoryToolIdentity.CALL_ONLY
    )


def operation_error(status: TrajectoryStatus, failure: TrajectoryFailure | None) -> bool:
    """Whether a terminal span must carry OpenTelemetry error status."""
    return failure is not None or status in FAILURE_STATUSES


def operation_error_type(status: TrajectoryStatus, failure: TrajectoryFailure | None) -> str | None:
    """Return explicit failure category or the terminal non-success status."""
    if failure is not None:
        return failure.category.value
    return status.value if status in FAILURE_STATUSES else None


__all__ = [
    "FAILURE_STATUSES",
    "RESULTS",
    "TERMINAL_STATUSES",
    "final_tool_operation",
    "operation_error",
    "operation_error_type",
]
