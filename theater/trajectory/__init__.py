"""Process-neutral trajectory domain values and pure projections."""

from __future__ import annotations

from theater.trajectory.bounds import bounded_preview, clip_utf8
from theater.trajectory.capabilities import (
    TrajectoryCapabilities,
    TrajectoryFeature,
    TrajectorySupport,
)
from theater.trajectory.content import (
    ContentFormat,
    ContentPreview,
    DetailField,
    bound_detail_fields,
    escape_rich_text,
    sanitize_text,
)
from theater.trajectory.enums import (
    CostProvenance,
    GroupKind,
    LinkDirection,
    PanelState,
    TimingProvenance,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryParticipantState,
    TrajectoryStatus,
    TrajectoryValidationError,
)
from theater.trajectory.location import TrajectoryLocation, TrajectoryLocationResolution
from theater.trajectory.overview import (
    TrajectoryCurrentOperation,
    TrajectoryErrorDiagnostics,
    TrajectoryIncompleteReason,
    TrajectoryOverview,
    TrajectoryProblem,
    TrajectorySlowOperation,
)
from theater.trajectory.page import (
    CoverageGap,
    PanelStateInfo,
    TrajectoryCoverage,
    TrajectoryDelta,
    TrajectoryGroup,
    TrajectoryPage,
    TrajectoryUpsert,
)
from theater.trajectory.records import (
    ParticipantLink,
    Timing,
    TrajectoryFailure,
    TrajectoryRecord,
    TrajectoryUsage,
)
from theater.trajectory.requests import (
    TrajectoryRequest,
    TrajectoryRequestIdentity,
    requests_for_records,
)
from theater.trajectory.tools import (
    TrajectoryToolIdentity,
    TrajectoryToolOperation,
    tool_operations_for_records,
)
from theater.trajectory.wire import from_wire, to_wire

__all__ = [
    "ContentFormat",
    "ContentPreview",
    "CostProvenance",
    "CoverageGap",
    "DetailField",
    "GroupKind",
    "LinkDirection",
    "PanelState",
    "PanelStateInfo",
    "ParticipantLink",
    "Timing",
    "TimingProvenance",
    "TrajectoryCapabilities",
    "TrajectoryCoverage",
    "TrajectoryCurrentOperation",
    "TrajectoryDelta",
    "TrajectoryErrorDiagnostics",
    "TrajectoryFailure",
    "TrajectoryFailureCategory",
    "TrajectoryFeature",
    "TrajectoryGroup",
    "TrajectoryIncompleteReason",
    "TrajectoryKind",
    "TrajectoryLane",
    "TrajectoryLocation",
    "TrajectoryLocationResolution",
    "TrajectoryOverview",
    "TrajectoryPage",
    "TrajectoryParticipantState",
    "TrajectoryProblem",
    "TrajectoryRecord",
    "TrajectoryRequest",
    "TrajectoryRequestIdentity",
    "TrajectorySlowOperation",
    "TrajectoryStatus",
    "TrajectorySupport",
    "TrajectoryToolIdentity",
    "TrajectoryToolOperation",
    "TrajectoryUpsert",
    "TrajectoryUsage",
    "TrajectoryValidationError",
    "bound_detail_fields",
    "bounded_preview",
    "clip_utf8",
    "deduplicate_records",
    "deterministic_record_order",
    "escape_rich_text",
    "event_to_fact",
    "event_to_record",
    "fact_to_record",
    "fallback_record_id",
    "from_wire",
    "group_records",
    "merge_records",
    "newer_record",
    "project_events",
    "project_facts",
    "record_id_for_fact",
    "requests_for_records",
    "sanitize_text",
    "to_wire",
    "tool_operations_for_records",
]


def __getattr__(name: str):
    if name in {
        "event_to_fact",
        "event_to_record",
        "fallback_record_id",
        "fact_to_record",
        "project_events",
        "project_facts",
        "record_id_for_fact",
    }:
        from theater.trajectory import projection

        return getattr(projection, name)
    if name in {
        "deduplicate_records",
        "deterministic_record_order",
        "group_records",
        "merge_records",
        "newer_record",
    }:
        from theater.trajectory import grouping

        return getattr(grouping, name)
    raise AttributeError(name)
