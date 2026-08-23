"""Bounded, process-local participant presentation state."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from math import isfinite

from theater.regie.trajectory.constants import (
    DEFAULT_INSPECTOR_RATIO,
    MAX_IDENTIFIER_BYTES,
    MAX_INSPECTOR_RATIO,
    MAX_LOADED_BYTES,
    MAX_LOADED_RECORDS,
    MAX_PARTICIPANT_STATES,
    MAX_QUERY_BYTES,
    MIN_INSPECTOR_RATIO,
)
from theater.regie.trajectory.enums import (
    FocusRegion,
    InspectorTab,
    Lane,
    OrderMode,
    PanelStatus,
    RecordKind,
    RecordStatus,
)
from theater.regie.trajectory.wire import (
    Coverage,
    GroupMetadata,
    PanelInfo,
    TrajectoryFollow,
    TrajectoryPage,
    TrajectoryRecord,
    clip_utf8,
)


def _record_size(record: TrajectoryRecord) -> int:
    return record.estimated_bytes


@dataclass(slots=True)
class ParticipantTrajectoryState:
    """Bounded mutable UI state for one participant; never persisted."""

    participant_id: str
    panel: PanelInfo = field(default_factory=lambda: PanelInfo(PanelStatus.LOADING))
    stream_id: str | None = None
    cursor: str | None = None
    older_cursor: str | None = None
    has_older: bool = False
    coverage: Coverage = field(default_factory=Coverage)
    groups: tuple[GroupMetadata, ...] = ()
    records: OrderedDict[str, TrajectoryRecord] = field(default_factory=OrderedDict)
    loaded_bytes: int = 0
    follow_tail: bool = True
    new_count: int = 0
    selected_id: str | None = None
    hovered_id: str | None = None
    collapsed_groups: set[str] = field(default_factory=set)
    query: str = ""
    lane_filters: set[Lane] = field(default_factory=set)
    kind_filters: set[RecordKind] = field(default_factory=set)
    status_filters: set[RecordStatus] = field(default_factory=set)
    source_filters: set[str] = field(default_factory=set)
    order_mode: OrderMode = OrderMode.ORDER
    timeline_scroll: int = 0
    inspector_tab: InspectorTab = InspectorTab.SUMMARY
    inspector_ratio: float = DEFAULT_INSPECTOR_RATIO
    inspector_maximized: bool = False
    inspector_open: bool = False
    focus_region: FocusRegion = FocusRegion.LEDGER
    stale: bool = False
    stale_message: str = ""
    retry_kind: str | None = None
    retry_message: str = ""
    reload_required: bool = False
    truncated_by_bytes: bool = False
    loading_older: bool = False
    search_open: bool = False
    filters_open: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.participant_id, str) or not self.participant_id:
            raise ValueError("participant_id must be a non-empty string")
        if len(self.participant_id.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
            raise ValueError("participant_id is too large")
        if (
            not isinstance(self.inspector_ratio, (int, float))
            or isinstance(self.inspector_ratio, bool)
            or not isfinite(float(self.inspector_ratio))
        ):
            raise ValueError("inspector ratio must be finite")
        self.inspector_ratio = max(
            MIN_INSPECTOR_RATIO, min(MAX_INSPECTOR_RATIO, float(self.inspector_ratio))
        )
        if not isinstance(self.timeline_scroll, int) or isinstance(self.timeline_scroll, bool):
            raise TypeError("timeline scroll must be an integer")
        self.timeline_scroll = max(0, self.timeline_scroll)
        if len(self.query.encode("utf-8")) > MAX_QUERY_BYTES:
            self.query = clip_utf8(self.query, MAX_QUERY_BYTES)[0]

    @property
    def record_list(self) -> list[TrajectoryRecord]:
        return list(self.records.values())

    @property
    def selected_record(self) -> TrajectoryRecord | None:
        return self.records.get(self.selected_id) if self.selected_id is not None else None

    @property
    def at_tail(self) -> bool:
        return self.follow_tail

    def _set_panel(self, panel: PanelInfo) -> None:
        self.panel = panel
        self.stale = panel.status == PanelStatus.STALE
        if not self.stale:
            self.stale_message = ""

    def _trim(self, *, evict_oldest: bool) -> None:
        while len(self.records) > MAX_LOADED_RECORDS or self.loaded_bytes > MAX_LOADED_BYTES:
            if not self.records:
                self.loaded_bytes = 0
                break
            record_id, record = self.records.popitem(last=evict_oldest)
            self.loaded_bytes -= _record_size(record)
            self.reload_required = True
            if record_id == self.selected_id:
                self.selected_id = None
            if record_id == self.hovered_id:
                self.hovered_id = None

    def upsert(
        self, records: Sequence[TrajectoryRecord], *, older: bool = False
    ) -> tuple[int, int]:
        """Apply records by stable ID and revision, returning (added, updated)."""
        added = 0
        updated = 0
        older_added: list[TrajectoryRecord] = []
        candidates: OrderedDict[str, TrajectoryRecord] = OrderedDict()
        for record in records:
            if record.participant_id == self.participant_id and (
                record.record_id not in candidates
                or record.revision > candidates[record.record_id].revision
            ):
                candidates[record.record_id] = record
        for record in candidates.values():
            existing = self.records.get(record.record_id)
            if existing is not None and record.revision <= existing.revision:
                continue
            if existing is not None:
                self.loaded_bytes -= _record_size(existing)
                self.records[record.record_id] = record
                updated += 1
            else:
                if older:
                    older_added.append(record)
                else:
                    self.records[record.record_id] = record
                added += 1
            self.loaded_bytes += _record_size(record)
        if older_added:
            self.records = OrderedDict(
                [(record.record_id, record) for record in older_added] + list(self.records.items())
            )
        self._trim(evict_oldest=older)
        if self.selected_id is None and self.records and self.follow_tail:
            self.selected_id = next(reversed(self.records))
        return added, updated

    def apply_snapshot(self, page: TrajectoryPage) -> None:
        """Replace loaded records with a validated snapshot."""
        if page.participant_id != self.participant_id:
            raise ValueError("snapshot participant does not match runtime state")
        prior_selection = self.selected_id
        self.records.clear()
        self.loaded_bytes = 0
        self.stream_id = page.stream_id
        self.cursor = page.cursor
        self.older_cursor = page.older_cursor
        self.has_older = page.has_older
        self.coverage = page.coverage
        self.groups = page.groups
        self.reload_required = False
        self.truncated_by_bytes = page.truncated_by_bytes
        self.loading_older = False
        self.retry_kind = None
        self.retry_message = ""
        self._set_panel(page.panel)
        self.upsert(page.records)
        if prior_selection in self.records:
            self.selected_id = prior_selection
        elif self.records:
            self.selected_id = next(reversed(self.records))
        else:
            self.selected_id = None

    def apply_older(self, page: TrajectoryPage) -> None:
        if page.participant_id != self.participant_id:
            raise ValueError("older page participant does not match runtime state")
        self.loading_older = False
        self.older_cursor = page.older_cursor
        self.has_older = page.has_older
        self.coverage = page.coverage
        known_groups = {group.group_id: group for group in self.groups}
        known_groups.update({group.group_id: group for group in page.groups})
        self.groups = tuple(known_groups.values())
        self.truncated_by_bytes = page.truncated_by_bytes
        self.retry_kind = None
        self.retry_message = ""
        self.upsert(page.records, older=True)

    def apply_follow(self, delta: TrajectoryFollow) -> tuple[int, int]:
        """Apply a follow delta without moving a paused tail."""
        if delta.participant_id != self.participant_id:
            raise ValueError("follow participant does not match runtime state")
        if delta.stream_id is not None:
            self.stream_id = delta.stream_id
        if delta.cursor is not None:
            self.cursor = delta.cursor
        if delta.panel is not None:
            self._set_panel(delta.panel)
        added, updated = self.upsert(delta.upserts)
        if added and not self.follow_tail:
            self.new_count += added
        elif added and self.follow_tail:
            self.new_count = 0
            self.selected_id = next(reversed(self.records))
        self.retry_kind = None
        self.retry_message = ""
        return added, updated

    def mark_stale(self, message: str) -> None:
        self.stale = True
        self.stale_message = message
        self.panel = PanelInfo(PanelStatus.STALE, message, retryable=True)
        self.retry_kind = "refresh"
        self.retry_message = message

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
            self.selected_id = next(reversed(self.records))

    def select(self, record_id: str | None) -> bool:
        if record_id is None:
            self.selected_id = None
            return True
        if record_id not in self.records:
            return False
        self.selected_id = record_id
        return True

    def move_selection(self, delta: int, visible_ids: Sequence[str] | None = None) -> str | None:
        ids = list(visible_ids) if visible_ids is not None else list(self.records)
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
        if delta < 0:
            self.pause_follow()
        return self.selected_id

    def set_ratio(self, ratio: float) -> float:
        if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not isfinite(ratio):
            raise ValueError("inspector ratio must be finite")
        self.inspector_ratio = max(MIN_INSPECTOR_RATIO, min(MAX_INSPECTOR_RATIO, float(ratio)))
        return self.inspector_ratio

    def reset_ui(self) -> None:
        """Reset only this participant's in-memory presentation state."""
        self.query = ""
        self.lane_filters.clear()
        self.kind_filters.clear()
        self.status_filters.clear()
        self.source_filters.clear()
        self.collapsed_groups.clear()
        self.selected_id = next(reversed(self.records), None)
        self.hovered_id = None
        self.order_mode = OrderMode.ORDER
        self.timeline_scroll = 0
        self.inspector_tab = InspectorTab.SUMMARY
        self.inspector_maximized = False
        self.inspector_open = False
        self.focus_region = FocusRegion.LEDGER
        self.search_open = False
        self.filters_open = False
        self.follow_tail = True
        self.new_count = 0
        self.retry_kind = None
        self.retry_message = ""


class TrajectoryStateStore:
    """Small LRU of participant UI state with no persistence hooks."""

    def __init__(self, *, max_participants: int = MAX_PARTICIPANT_STATES) -> None:
        if max_participants < 1:
            raise ValueError("max_participants must be positive")
        self.max_participants = max_participants
        self._states: OrderedDict[str, ParticipantTrajectoryState] = OrderedDict()

    def get(self, participant_id: str) -> ParticipantTrajectoryState:
        if not isinstance(participant_id, str) or not participant_id:
            raise ValueError("participant_id must be a non-empty string")
        if len(participant_id.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
            raise ValueError("participant_id is too large")
        if participant_id in self._states:
            state = self._states.pop(participant_id)
            self._states[participant_id] = state
            return state
        state = ParticipantTrajectoryState(participant_id)
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
