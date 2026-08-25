"""Bounded, process-local participant presentation state."""

from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import isfinite

from theater.regie.trajectory.constants import (
    TRAJECTORY_DETAIL_FIELD_MAX_BYTES,
    TRAJECTORY_IDENTIFIER_MAX_BYTES,
    TRAJECTORY_INSPECTOR_RATIO_DEFAULT,
    TRAJECTORY_INSPECTOR_RATIO_MAX,
    TRAJECTORY_INSPECTOR_RATIO_MIN,
    TRAJECTORY_LEDGER_PAGE_SIZE_DEFAULT,
    TRAJECTORY_LEDGER_PAGE_SIZE_MAX,
    TRAJECTORY_UI_MAX_BYTES,
    TRAJECTORY_UI_RECORD_LIMIT,
    TRAJECTORY_WARM_STREAM_LIMIT,
)
from theater.regie.trajectory.diagnostic_views import (
    DiagnosticIndex,
    build_diagnostic_index,
    empty_diagnostic_index,
)
from theater.regie.trajectory.enums import DiagnosticView, FocusRegion, InspectorTab, OrderMode
from theater.regie.trajectory.ordering import canonical_group_records
from theater.regie.trajectory.request_rows import (
    RequestIndex,
    build_request_index,
    empty_request_index,
)
from theater.regie.trajectory.tool_rows import ToolIndex, build_tool_index, empty_tool_index
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryCapabilities,
    TrajectoryCoverage,
    TrajectoryDelta,
    TrajectoryGroup,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryOverview,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryValidationError,
    deterministic_record_order,
    group_records,
)


def _record_size(record: TrajectoryRecord) -> int:
    return len(
        json.dumps(record.to_wire(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


@dataclass(slots=True)
class ParticipantTrajectoryState:
    """Bounded mutable UI state for one participant; never persisted."""

    participant_id: str
    panel: PanelStateInfo = field(
        default_factory=lambda: PanelStateInfo(PanelState.WAITING, "Loading trajectory…")
    )
    stream_id: str | None = None
    cursor: str | None = None
    older_cursor: str | None = None
    has_older: bool = False
    coverage: TrajectoryCoverage = field(default_factory=TrajectoryCoverage)
    capabilities: TrajectoryCapabilities = field(default_factory=TrajectoryCapabilities)
    overview: TrajectoryOverview = field(default_factory=TrajectoryOverview)
    groups: tuple[TrajectoryGroup, ...] = ()
    records: OrderedDict[str, TrajectoryRecord] = field(default_factory=OrderedDict)
    request_index: RequestIndex = field(default_factory=empty_request_index)
    tool_index: ToolIndex = field(default_factory=empty_tool_index)
    diagnostic_index: DiagnosticIndex = field(default_factory=empty_diagnostic_index)
    loaded_bytes: int = 0
    follow_tail: bool = True
    new_count: int = 0
    selected_id: str | None = None
    hovered_id: str | None = None
    query: str = ""
    lane_filters: set[TrajectoryLane] = field(default_factory=set)
    kind_filters: set[TrajectoryKind] = field(default_factory=set)
    status_filters: set[TrajectoryStatus] = field(default_factory=set)
    source_filters: set[str] = field(default_factory=set)
    diagnostic_view: DiagnosticView = DiagnosticView.ALL
    order_mode: OrderMode = OrderMode.ORDER
    ledger_page: int = 0
    timeline_scroll: int = 0
    expanded_id: str | None = None
    detail_tab: InspectorTab = InspectorTab.SUMMARY
    detail_ratio: float = TRAJECTORY_INSPECTOR_RATIO_DEFAULT
    focus_region: FocusRegion = FocusRegion.LEDGER
    stale: bool = False
    stale_message: str = ""
    retry_kind: str | None = None
    retry_message: str = ""
    resyncing: bool = False
    reload_required: bool = False
    truncated_by_bytes: bool = False
    loading_older: bool = False
    loading: bool = True
    search_open: bool = False
    filters_open: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.participant_id, str) or not self.participant_id:
            raise ValueError("participant_id must be a non-empty string")
        if len(self.participant_id.encode("utf-8")) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
            raise ValueError("participant_id is too large")
        self.set_detail_ratio(self.detail_ratio)
        if not isinstance(self.ledger_page, int) or isinstance(self.ledger_page, bool):
            raise TypeError("ledger page must be an integer")
        self.ledger_page = max(0, self.ledger_page)
        if not isinstance(self.timeline_scroll, int) or isinstance(self.timeline_scroll, bool):
            raise TypeError("timeline scroll must be an integer")
        self.timeline_scroll = max(0, self.timeline_scroll)
        if len(self.query.encode("utf-8")) > TRAJECTORY_DETAIL_FIELD_MAX_BYTES:
            self.query = self.query.encode("utf-8")[:TRAJECTORY_DETAIL_FIELD_MAX_BYTES].decode(
                "utf-8", errors="ignore"
            )

    @property
    def record_list(self) -> list[TrajectoryRecord]:
        return list(canonical_group_records(tuple(self.records.values()), self.groups))

    @property
    def selected_record(self) -> TrajectoryRecord | None:
        return self.records.get(self.selected_id) if self.selected_id is not None else None

    def _set_panel(self, panel: PanelStateInfo) -> None:
        self.panel = panel
        self.stale = panel.state is PanelState.STALE
        if not self.stale:
            self.stale_message = ""

    def apply_panel_state(self, panel: PanelStateInfo) -> None:
        """Apply a live panel update without changing loaded records."""
        self._set_panel(panel)
        if panel.state in {PanelState.STALE, PanelState.UNAVAILABLE, PanelState.UNTRUSTED}:
            self.retry_kind = "refresh"
            self.retry_message = panel.message or "Retry trajectory refresh."
        elif self.retry_kind != "resync":
            self.retry_kind = None
            self.retry_message = ""

    def _rebuild_groups(self) -> None:
        self.groups = group_records(self.record_list)
        self.request_index = build_request_index(self.records.values())
        self.tool_index = build_tool_index(self.records.values())
        self.diagnostic_index = build_diagnostic_index(
            self.records.values(), self.request_index, self.tool_index
        )

    def row_anchor(self, record_id: str | None) -> str | None:
        """Resolve a tool member to its canonical ledger row anchor."""
        if record_id is None:
            return None
        operation_id = self.tool_index.by_record_id.get(record_id)
        if operation_id is not None:
            return self.tool_index.anchor_by_id.get(operation_id)
        return record_id if record_id in self.records else None

    def related_record_ids(self, record_id: str | None) -> frozenset[str]:
        """Return retained records in the same request or tool operation."""
        if record_id is None or record_id not in self.records:
            return frozenset()
        related: set[str] = set()
        request_id = self.request_index.by_record_id.get(record_id)
        if request_id is not None:
            request = self.request_index.by_id.get(request_id)
            if request is not None:
                related.update(request.record_ids)
        operation_id = self.tool_index.by_record_id.get(record_id)
        if operation_id is not None:
            related.update(self.tool_index.members_by_id.get(operation_id, ()))
        related.discard(record_id)
        return frozenset(candidate for candidate in related if candidate in self.records)

    def _trim(self, *, evict_newest: bool) -> None:
        while (
            len(self.records) > TRAJECTORY_UI_RECORD_LIMIT
            or self.loaded_bytes > TRAJECTORY_UI_MAX_BYTES
        ):
            if not self.records:
                self.loaded_bytes = 0
                break
            record_id, record = self.records.popitem(last=evict_newest)
            self.loaded_bytes -= _record_size(record)
            self.reload_required = True
            if record_id == self.selected_id:
                self.selected_id = None
            if record_id == self.hovered_id:
                self.hovered_id = None
            if record_id == self.expanded_id:
                self.expanded_id = None

    def _apply_records(
        self, records: Sequence[TrajectoryRecord], *, older: bool = False
    ) -> tuple[int, int]:
        added = 0
        updated = 0
        candidates: OrderedDict[str, TrajectoryRecord] = OrderedDict()
        for record in records:
            if not isinstance(record, TrajectoryRecord):
                raise TrajectoryValidationError("runtime records must contain trajectory records")
            if record.participant_id != self.participant_id:
                raise TrajectoryValidationError("record participant does not match runtime state")
            current = candidates.get(record.record_id)
            if current is None or record.revision > current.revision:
                candidates[record.record_id] = record
        older_added: list[TrajectoryRecord] = []
        for record in candidates.values():
            existing = self.records.get(record.record_id)
            if existing is not None and record.revision <= existing.revision:
                continue
            if existing is not None:
                self.loaded_bytes -= _record_size(existing)
                self.records[record.record_id] = record
                updated += 1
            elif older:
                older_added.append(record)
                added += 1
            else:
                self.records[record.record_id] = record
                added += 1
            self.loaded_bytes += _record_size(record)
        if older_added:
            self.records = OrderedDict(
                [(record.record_id, record) for record in older_added] + list(self.records.items())
            )
        self.records = OrderedDict(
            (record.record_id, record)
            for record in deterministic_record_order(self.records.values())
        )
        self._trim(evict_newest=older)
        self._rebuild_groups()
        if self.selected_id is None and self.records and self.follow_tail:
            self.selected_id = self.record_list[-1].record_id
        return added, updated

    def upsert(
        self, records: Sequence[TrajectoryRecord], *, older: bool = False
    ) -> tuple[int, int]:
        """Apply records by stable ID and revision, returning added and updated counts."""
        return self._apply_records(records, older=older)

    def apply_snapshot(self, page: TrajectoryPage) -> None:
        """Replace loaded records with a validated canonical snapshot."""
        prior_selection = self.selected_id
        prior_records = OrderedDict(self.records)
        prior_bytes = self.loaded_bytes
        prior_groups = self.groups
        prior_request_index = self.request_index
        prior_tool_index = self.tool_index
        prior_diagnostic_index = self.diagnostic_index
        preserve_trace = (
            page.panel_state.state
            in {PanelState.STALE, PanelState.UNAVAILABLE, PanelState.UNTRUSTED}
            and bool(self.records)
            and not page.records
        )
        self.records.clear()
        self.loaded_bytes = 0
        self.stream_id = page.stream_id
        self.cursor = page.cursor
        self.older_cursor = page.older_cursor
        self.has_older = page.has_older
        self.coverage = page.coverage
        self.capabilities = page.capabilities
        self.overview = page.overview
        self.reload_required = False
        self.truncated_by_bytes = page.truncated_by_bytes
        self.loading_older = False
        self.loading = False
        self.resyncing = False
        self.retry_kind = None
        self.retry_message = ""
        if preserve_trace:
            self.records = prior_records
            self.loaded_bytes = prior_bytes
            self.groups = prior_groups
            self.request_index = prior_request_index
            self.tool_index = prior_tool_index
            self.diagnostic_index = prior_diagnostic_index
        else:
            self._apply_records(page.records)
            self.groups = page.groups or self.groups
        self.apply_panel_state(page.panel_state)
        if self.follow_tail and self.records:
            self.selected_id = self.record_list[-1].record_id
        elif prior_selection in self.records:
            self.selected_id = prior_selection
        elif self.records:
            self.selected_id = next(reversed(self.records))
        else:
            self.selected_id = None
        if self.row_anchor(self.expanded_id) is None:
            self.expanded_id = None

    def apply_older(self, page: TrajectoryPage) -> None:
        if page.stream_id is not None and self.stream_id not in {None, page.stream_id}:
            raise TrajectoryValidationError("older page stream does not match runtime state")
        self.loading_older = False
        if page.stream_id is not None:
            self.stream_id = page.stream_id
        self.older_cursor = page.older_cursor
        self.has_older = page.has_older
        self.coverage = page.coverage
        self.capabilities = page.capabilities
        self.overview = page.overview
        self.truncated_by_bytes = page.truncated_by_bytes
        self._set_panel(page.panel_state)
        self.retry_kind = None
        self.retry_message = ""
        if page.panel_state.state in {
            PanelState.STALE,
            PanelState.UNAVAILABLE,
            PanelState.UNTRUSTED,
        }:
            self.retry_kind = "older"
            self.retry_message = page.panel_state.message or "Retry older trajectory page."
        self._apply_records(page.records, older=True)

    def apply_follow(self, delta: TrajectoryDelta) -> tuple[int, int]:
        """Apply canonical follow upserts without moving a paused tail."""
        if self.stream_id is not None and delta.stream_id != self.stream_id:
            raise TrajectoryValidationError("follow stream does not match runtime state")
        self.stream_id = delta.stream_id
        if delta.cursor is not None:
            self.cursor = delta.cursor
        capabilities = getattr(delta, "capabilities", None)
        if capabilities is not None:
            self.capabilities = capabilities
        overview = getattr(delta, "overview", None)
        if overview is not None:
            self.overview = overview
        panel = getattr(delta, "panel_state", None)
        if panel is not None:
            if not isinstance(panel, PanelStateInfo):
                raise TrajectoryValidationError("follow panel state is invalid")
            self.apply_panel_state(panel)
        added, updated = self._apply_records([upsert.record for upsert in delta.upserts])
        if added and not self.follow_tail:
            self.new_count += added
        elif added:
            self.new_count = 0
            self.selected_id = self.record_list[-1].record_id
        if panel is None and self.retry_kind != "resync":
            self.retry_kind = None
            self.retry_message = ""
        return added, updated

    def mark_stale(self, message: str) -> None:
        self.stale = True
        self.stale_message = message
        self.panel = PanelStateInfo(
            PanelState.STALE,
            message,
            participant_state=self.panel.participant_state,
        )
        self.retry_kind = "refresh"
        self.retry_message = message
        self.loading = False

    def mark_retry(self, kind: str, message: str) -> None:
        self.retry_kind = kind
        self.retry_message = message
        self.loading_older = False

    def mark_resync(self, message: str = "The trajectory stream changed; resync required.") -> None:
        self.reload_required = True
        self.mark_stale(message)
        self.retry_kind = "resync"

    def pause_follow(self) -> None:
        self.follow_tail = False

    def resume_follow(self) -> None:
        self.follow_tail = True
        self.new_count = 0
        if self.records:
            self.selected_id = self.record_list[-1].record_id

    def select(self, record_id: str | None) -> bool:
        if record_id is None:
            self.selected_id = None
            return True
        if record_id not in self.records:
            return False
        self.selected_id = record_id
        return True

    def move_selection(self, delta: int, visible_ids: Sequence[str] | None = None) -> str | None:
        ids = (
            list(visible_ids)
            if visible_ids is not None
            else [record.record_id for record in self.record_list]
        )
        if not ids:
            self.selected_id = None
            return None
        current = (
            ids.index(self.selected_id)
            if self.selected_id in ids
            else (len(ids) - 1 if delta > 0 else 0)
        )
        target = max(0, min(len(ids) - 1, current + delta))
        self.selected_id = ids[target]
        if target == len(ids) - 1:
            self.follow_tail = True
            self.new_count = 0
        else:
            self.pause_follow()
        return self.selected_id

    def set_detail_ratio(self, ratio: float) -> float:
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not isfinite(ratio):
            raise ValueError("detail ratio must be finite")
        self.detail_ratio = max(
            TRAJECTORY_INSPECTOR_RATIO_MIN,
            min(TRAJECTORY_INSPECTOR_RATIO_MAX, float(ratio)),
        )
        return self.detail_ratio

    def reset_ui(self) -> None:
        """Reset only this participant's in-memory presentation state."""
        resync_pending = self.retry_kind == "resync"
        resync_message = self.retry_message
        self.query = ""
        self.lane_filters.clear()
        self.kind_filters.clear()
        self.status_filters.clear()
        self.source_filters.clear()
        self.diagnostic_view = DiagnosticView.ALL
        self.selected_id = None
        self.hovered_id = None
        self.order_mode = OrderMode.ORDER
        self.ledger_page = 0
        self.timeline_scroll = 0
        self.expanded_id = None
        self.detail_tab = InspectorTab.SUMMARY
        self.focus_region = FocusRegion.LEDGER
        self.search_open = False
        self.filters_open = False
        self.follow_tail = True
        self.new_count = 0
        self.retry_kind = "resync" if resync_pending else None
        self.retry_message = resync_message if resync_pending else ""


class TrajectoryStateStore:
    """Small LRU of participant UI state with no persistence hooks."""

    def __init__(
        self,
        *,
        max_participants: int = TRAJECTORY_WARM_STREAM_LIMIT,
        detail_ratio: float = TRAJECTORY_INSPECTOR_RATIO_DEFAULT,
        page_size: int = TRAJECTORY_LEDGER_PAGE_SIZE_DEFAULT,
    ) -> None:
        if max_participants < 1:
            raise ValueError("max_participants must be positive")
        if (
            not isinstance(detail_ratio, (int, float))
            or isinstance(detail_ratio, bool)
            or not isfinite(detail_ratio)
        ):
            raise ValueError("detail ratio must be finite")
        if (
            not isinstance(page_size, int)
            or isinstance(page_size, bool)
            or not 1 <= page_size <= TRAJECTORY_LEDGER_PAGE_SIZE_MAX
        ):
            raise ValueError("page size is outside the supported range")
        self.max_participants = max_participants
        self.detail_ratio = max(
            TRAJECTORY_INSPECTOR_RATIO_MIN,
            min(TRAJECTORY_INSPECTOR_RATIO_MAX, float(detail_ratio)),
        )
        self.page_size = page_size
        self._states: OrderedDict[str, ParticipantTrajectoryState] = OrderedDict()

    def get(self, participant_id: str) -> ParticipantTrajectoryState:
        if not isinstance(participant_id, str) or not participant_id:
            raise ValueError("participant_id must be a non-empty string")
        if len(participant_id.encode("utf-8")) > TRAJECTORY_IDENTIFIER_MAX_BYTES:
            raise ValueError("participant_id is too large")
        if participant_id in self._states:
            state = self._states.pop(participant_id)
            self._states[participant_id] = state
            return state
        state = ParticipantTrajectoryState(
            participant_id,
            detail_ratio=self.detail_ratio,
        )
        self._states[participant_id] = state
        while len(self._states) > self.max_participants:
            self._states.popitem(last=False)
        return state

    def peek(self, participant_id: str) -> ParticipantTrajectoryState | None:
        return self._states.get(participant_id)

    def __len__(self) -> int:
        return len(self._states)

    def participant_ids(self) -> tuple[str, ...]:
        return tuple(self._states)


__all__ = ["ParticipantTrajectoryState", "TrajectoryStateStore"]
