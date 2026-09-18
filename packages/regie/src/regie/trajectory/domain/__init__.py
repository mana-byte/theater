"""Process-neutral trajectory domain values and pure projections."""

from __future__ import annotations

from regie.trajectory.domain.bounds import bounded_preview, clip_utf8
from regie.trajectory.domain.capabilities import (
    TrajectoryCapabilities,
    TrajectoryFeature,
    TrajectorySupport,
)
from regie.trajectory.domain.content import (
    ContentFormat,
    ContentPreview,
    DetailField,
    bound_detail_fields,
    escape_rich_text,
    sanitize_text,
)
from regie.trajectory.domain.enums import (
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
from regie.trajectory.domain.grouping import (
    deduplicate_records,
    deterministic_record_order,
    group_records,
    merge_records,
    newer_record,
)
from regie.trajectory.domain.identity import fallback_record_id
from regie.trajectory.domain.location import TrajectoryLocation, TrajectoryLocationResolution
from regie.trajectory.domain.overview import (
    TrajectoryCurrentOperation,
    TrajectoryErrorDiagnostics,
    TrajectoryIncompleteReason,
    TrajectoryOverview,
    TrajectoryProblem,
    TrajectorySlowOperation,
)
from regie.trajectory.domain.page import (
    CoverageGap,
    PanelStateInfo,
    TrajectoryCoverage,
    TrajectoryDelta,
    TrajectoryGroup,
    TrajectoryPage,
    TrajectoryUpsert,
)
from regie.trajectory.domain.records import (
    ParticipantLink,
    Timing,
    TrajectoryFailure,
    TrajectoryRecord,
    TrajectoryUsage,
)
from regie.trajectory.domain.requests import (
    TrajectoryRequest,
    TrajectoryRequestIdentity,
    requests_for_records,
)
from regie.trajectory.domain.search import (
    TrajectorySearchResult,
    fuzzy_subsequence_score,
    ranked_records,
    record_search_fields,
    record_search_score,
    record_search_text,
)
from regie.trajectory.domain.tools import (
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
