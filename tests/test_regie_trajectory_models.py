from __future__ import annotations

import pytest

from theater.regie.trajectory.models import (
    MAX_DETAIL_BYTES,
    MAX_LOADED_RECORDS,
    ContentFormat,
    ContentPreview,
    PanelStatus,
    RecordKind,
    RecordStatus,
    TrajectoryFollow,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryStateStore,
    WireDecodeError,
    clip_utf8,
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
    details: object | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "record_id": record_id,
        "revision": revision,
        "participant_id": participant_id,
        "lane": lane,
        "kind": kind,
        "source": "claude",
        "summary": summary,
        "status": "completed",
    }
    if turn_id is not None:
        result["turn_id"] = turn_id
    if details is not None:
        result["details"] = details
    return result


def test_utf8_clip_preserves_ends_and_byte_cap() -> None:
    clipped, truncated, original = clip_utf8("début" + "界" * 10_000 + "fin", 128)

    assert truncated is True
    assert original > 128
    assert len(clipped.encode("utf-8")) <= 128
    assert clipped.startswith("début")
    assert clipped.endswith("fin")
    assert "bytes omitted" in clipped


def test_content_preview_rebounds_nested_json_and_unknown_format() -> None:
    preview = ContentPreview.from_wire(
        {"format": "vendor-format", "value": {"a": [1, True]}},
    )

    assert preview.format is ContentFormat.UNKNOWN
    assert '"a"' in preview.text
    assert ContentPreview.from_wire(preview.to_wire()) == preview


def test_record_wire_round_trip_and_unknown_enums_are_safe() -> None:
    record = TrajectoryRecord.from_wire(
        wire_record("r1", lane="future", kind="vendor_kind", summary="[bold]safe[/bold]")
    )

    assert record.lane.value == "unknown"
    assert record.kind is RecordKind.UNKNOWN
    assert record.status is RecordStatus.COMPLETED
    assert TrajectoryRecord.from_wire(record.to_wire()) == record


def test_malformed_wire_is_rejected_and_summary_is_bounded() -> None:
    with pytest.raises(WireDecodeError):
        TrajectoryRecord.from_wire({"record_id": "only-an-id"})

    record = TrajectoryRecord.from_wire(wire_record("r1", summary="x" * 100_000))
    assert len(record.summary.encode("utf-8")) <= 16 * 1024
    assert record.summary.startswith("x")
    assert "bytes omitted" in record.summary


def test_detail_aggregate_is_bounded() -> None:
    record = TrajectoryRecord.from_wire(
        wire_record("r1", details={f"field-{index}": "x" * 16_000 for index in range(64)})
    )

    total = sum(
        len(field.name.encode("utf-8")) + 2 + len(field.value.text.encode("utf-8"))
        for field in record.details
    )
    assert total <= MAX_DETAIL_BYTES


def test_page_caps_response_records_and_unknown_panel_state() -> None:
    page = TrajectoryPage.from_wire(
        {
            "participant_id": "p1",
            "panel": "future-state",
            "records": [wire_record(str(index), summary="x" * 16_000) for index in range(200)],
        }
    )

    assert page.panel.status is PanelStatus.UNKNOWN
    assert page.truncated_by_bytes
    assert len(page.records) < 200


def test_runtime_state_rejects_old_revision_and_preserves_older_order() -> None:
    first = TrajectoryRecord.from_wire(wire_record("r1", revision=1))
    newer = TrajectoryRecord.from_wire(wire_record("r1", revision=2, summary="new"))
    old = TrajectoryRecord.from_wire(wire_record("r0", turn_id=None))
    state = TrajectoryStateStore().get("p1")

    assert state.upsert([first]) == (1, 0)
    assert state.upsert([TrajectoryRecord.from_wire(wire_record("r1", revision=0))]) == (0, 0)
    assert state.upsert([newer]) == (0, 1)
    assert state.selected_record == newer
    state.upsert([old], older=True)

    assert list(state.records) == ["r0", "r1"]
    assert state.records["r1"].summary == "new"


def test_runtime_state_follow_tail_and_reset_are_local() -> None:
    state = TrajectoryStateStore().get("p1")
    state.upsert([TrajectoryRecord.from_wire(wire_record("r1"))])
    state.pause_follow()
    state.apply_follow(
        TrajectoryFollow.from_wire(
            {"participant_id": "p1", "cursor": "c2", "upserts": [wire_record("r2")]}
        )
    )

    assert state.new_count == 1
    assert state.selected_id == "r1"
    state.query = "r"
    state.reset_ui()
    assert state.query == ""
    assert state.follow_tail
    assert state.new_count == 0


def test_state_store_is_bounded_lru() -> None:
    store = TrajectoryStateStore(max_participants=2)
    store.get("p1")
    store.get("p2")
    store.get("p1")
    store.get("p3")

    assert store.participant_ids() == ("p1", "p3")
    assert len(store) == 2


def test_loaded_record_cap_evicts_far_edge() -> None:
    state = TrajectoryStateStore().get("p1")
    records = [
        TrajectoryRecord.from_wire(wire_record(str(index)))
        for index in range(MAX_LOADED_RECORDS + 1)
    ]

    state.upsert(records)

    assert len(state.records) == MAX_LOADED_RECORDS
    assert state.reload_required
    assert "0" not in state.records
