from __future__ import annotations

import json

import pytest

from theater.regie.trajectory.models import decode_delta, decode_page
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore
from theater.trajectory import (
    ContentFormat,
    DetailField,
    PanelState,
    PanelStateInfo,
    TrajectoryDelta,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryValidationError,
)


def wire_record(
    record_id: str,
    *,
    revision: int = 1,
    participant_id: str = "p1",
    lane: str = "model",
    kind: str = "assistant",
    summary: str = "summary",
    turn_id: str | None = "turn-1",
    step_id: str | None = None,
    details: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "record_id": record_id,
        "revision": revision,
        "participant_id": participant_id,
        "source_epoch": "epoch-1",
        "lane": lane,
        "kind": kind,
        "source": "claude",
        "summary": summary,
        "status": "completed",
    }
    if turn_id is not None:
        result["turn_id"] = turn_id
    if step_id is not None:
        result["step_id"] = step_id
    if details is not None:
        result["details"] = details
    return result


def page_wire(records: list[dict[str, object]], *, stream_id: str = "stream") -> dict[str, object]:
    return {
        "panel_state": {"state": "ready", "participant_state": "live"},
        "stream_id": stream_id,
        "cursor": "cursor-1",
        "records": records,
        "groups": [],
        "older_cursor": None,
        "has_older": False,
    }


def test_adapter_uses_canonical_page_and_delta_without_participant_fields() -> None:
    page = decode_page(page_wire([wire_record("r1")]))
    delta = decode_delta(
        {
            "stream_id": "stream",
            "cursor": "cursor-2",
            "upserts": [{"record": wire_record("r2")}],
        }
    )

    assert isinstance(page, TrajectoryPage)
    assert isinstance(delta, TrajectoryDelta)
    assert page.panel_state.state is PanelState.READY
    assert delta.upserts[0].record.record_id == "r2"
    with pytest.raises(TrajectoryValidationError):
        decode_page(page_wire([wire_record("r1")]) | {"participant_id": "p1"})


def test_canonical_detail_format_and_literal_content_are_preserved() -> None:
    record = TrajectoryRecord.from_wire(
        wire_record(
            "r1",
            summary="[literal] \\ path",
            details=[
                {
                    "name": "payload",
                    "format": "json",
                    "value": {"text": '{"b": 2, "a": 1}', "omitted_bytes": 0},
                }
            ],
        )
    )

    assert record.summary == "[literal] \\ path"
    assert record.details[0].format is ContentFormat.JSON
    assert record.details[0].preview.text.startswith("{")
    assert DetailField.from_text("x", "y").preview.text == "y"


def test_runtime_state_rejects_mixed_participant_and_keeps_revision_precedence() -> None:
    state = TrajectoryStateStore().get("p1")
    first = TrajectoryRecord.from_wire(wire_record("r1"))
    newer = TrajectoryRecord.from_wire(wire_record("r1", revision=2, summary="new"))
    other = TrajectoryRecord.from_wire(wire_record("other", participant_id="p2"))

    assert state.upsert([first]) == (1, 0)
    assert state.upsert([newer]) == (0, 1)
    assert state.records["r1"].summary == "new"
    with pytest.raises(TrajectoryValidationError):
        state.upsert([other])


def test_runtime_state_skips_rebuilding_indexes_for_unchanged_upserts(monkeypatch) -> None:
    state = TrajectoryStateStore().get("p1")
    item = TrajectoryRecord.from_wire(wire_record("r1"))
    state.upsert([item])
    rebuilds = 0
    original_rebuild = ParticipantTrajectoryState._rebuild_groups

    def count_rebuild(self: ParticipantTrajectoryState) -> None:
        nonlocal rebuilds
        rebuilds += 1
        original_rebuild(self)

    monkeypatch.setattr(ParticipantTrajectoryState, "_rebuild_groups", count_rebuild)

    assert state.upsert([item]) == (0, 0)
    assert rebuilds == 0


def test_runtime_state_repairs_tail_selection_for_unchanged_upserts() -> None:
    state = TrajectoryStateStore().get("p1")
    item = TrajectoryRecord.from_wire(wire_record("r1"))
    state.upsert([item])
    state.selected_id = None

    assert state.upsert([item]) == (0, 0)
    assert state.selected_id == "r1"


def test_empty_snapshot_rebuilds_indexes_after_replacing_loaded_records() -> None:
    state = TrajectoryStateStore().get("p1")
    item = TrajectoryRecord.from_wire(wire_record("r1", kind="tool_call"))
    state.upsert([item])
    assert state.groups
    assert state.tool_index.ordered

    state.apply_snapshot(TrajectoryPage(PanelStateInfo(PanelState.READY), records=()))

    assert not state.records
    assert not state.groups
    assert not state.request_index.by_id
    assert not state.tool_index.ordered
    assert all(
        not projection.record_ids for projection in state.diagnostic_index.by_view.values()
    )


def test_runtime_state_counts_compact_utf8_wire_bytes() -> None:
    state = TrajectoryStateStore().get("p1")
    item = TrajectoryRecord.from_wire(wire_record("r1", summary="régie"))

    state.upsert([item])

    encoded = json.dumps(item.to_wire(), ensure_ascii=False, separators=(",", ":")).encode()
    assert state.loaded_bytes == len(encoded)


def test_state_store_applies_configured_page_size() -> None:
    store = TrajectoryStateStore(page_size=17)

    assert store.get("p1").participant_id == "p1"
    assert store.get("p2").participant_id == "p2"
    assert store.page_size == 17
