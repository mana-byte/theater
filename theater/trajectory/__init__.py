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
from theater.trajectory.grouping import (
    deduplicate_records,
    deterministic_record_order,
    group_records,
    merge_records,
    newer_record,
)
from theater.trajectory.identity import fallback_record_id
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
from theater.trajectory.search import (
    TrajectorySearchResult,
    fuzzy_subsequence_score,
    ranked_records,
    record_search_fields,
    record_search_score,
    record_search_text,
)
from theater.trajectory.tools import (
    TrajectoryToolIdentity,
    TrajectoryToolOperation,
    tool_operations_for_records,
)

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
    "TrajectorySearchResult",
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
    "fallback_record_id",
    "fuzzy_subsequence_score",
    "group_records",
    "merge_records",
    "newer_record",
    "ranked_records",
    "record_search_fields",
    "record_search_score",
    "record_search_text",
    "requests_for_records",
    "sanitize_text",
    "tool_operations_for_records",
]
