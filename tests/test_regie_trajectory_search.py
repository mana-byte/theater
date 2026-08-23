from __future__ import annotations

from theater.regie.trajectory.models import Lane, RecordKind, RecordStatus, TrajectoryRecord
from theater.regie.trajectory.search import (
    TrajectoryFilters,
    fuzzy_subsequence_score,
    record_search_text,
    search_records,
)


def record(
    record_id: str,
    summary: str,
    *,
    lane: str = "model",
    kind: str = "assistant",
    source: str = "claude",
    status: str = "completed",
    turn_id: str | None = "t1",
) -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(
        {
            "record_id": record_id,
            "revision": 1,
            "participant_id": "p1",
            "lane": lane,
            "kind": kind,
            "source": source,
            "summary": summary,
            "status": status,
            "turn_id": turn_id,
            "details": {"call_id": record_id},
        }
    )


def test_subsequence_score_requires_order_but_does_not_order_results() -> None:
    assert fuzzy_subsequence_score("abc", "a---b---c") is not None
    assert fuzzy_subsequence_score("abc", "acb") is None
    assert fuzzy_subsequence_score("", "anything") == 0

    records = [record("1", "abc"), record("2", "a better abc")]
    result = search_records(records, query="abc")
    assert result.record_ids == ("1", "2")


def test_search_uses_bounded_ids_and_details() -> None:
    item = record("native-call", "unrelated", turn_id=None)
    assert "native-call" in record_search_text(item)
    result = search_records([item], query="native")
    assert result.record_ids == ("native-call",)


def test_filters_retain_structural_headers_and_report_counts() -> None:
    records = [
        record("input", "ask", lane="input", kind="user", turn_id="t1"),
        record("tool", "run", lane="tools", kind="tool_call", turn_id="t1", source="codex"),
        record("theater", "spawn", lane="theater", kind="spawn", turn_id=None, source="theater"),
    ]
    result = search_records(
        records,
        filters=TrajectoryFilters.from_sets(lanes=[Lane.TOOLS]),
    )

    assert result.record_ids == ("tool",)
    assert [entry.group_id for entry in result.entries] == ["turn:t1", "turn:t1"]
    assert result.entries[0].is_header
    assert result.entries[1].record_id == "tool"
    assert result.counts.lanes[Lane.TOOLS] == 1
    assert result.counts.kinds[RecordKind.TOOL_CALL] == 1
    assert result.counts.statuses[RecordStatus.COMPLETED] == 1


def test_collapsed_group_keeps_header_and_hides_rows() -> None:
    records = [record("one", "one"), record("two", "two")]

    result = search_records(records, collapsed_groups={"turn:t1"})

    assert len(result.entries) == 1
    assert result.entries[0].is_header
    assert result.entries[0].collapsed
