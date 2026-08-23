from __future__ import annotations

from theater.regie.trajectory.search import (
    TrajectoryFilters,
    fuzzy_subsequence_score,
    record_search_text,
    search_records,
)
from theater.trajectory import (
    GroupKind,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    group_records,
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
    step_id: str | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(
        {
            "record_id": record_id,
            "revision": 1,
            "participant_id": "p1",
            "source_epoch": "epoch",
            "lane": lane,
            "kind": kind,
            "source": source,
            "summary": summary,
            "status": status,
            "turn_id": turn_id,
            "step_id": step_id,
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


def test_filters_retain_nested_headers_and_report_counts() -> None:
    records = [
        record("input", "ask", lane="input", kind="user", step_id="s1"),
        record("tool", "run", lane="tools", kind="tool_call", source="codex", step_id="s1"),
        record("theater", "spawn", lane="theater", kind="spawn", source="theater", turn_id=None),
    ]
    result = search_records(
        records,
        filters=TrajectoryFilters.from_sets(lanes=[TrajectoryLane.TOOLS]),
    )

    assert result.record_ids == ("tool",)
    assert [entry.group_kind for entry in result.entries] == [
        GroupKind.TURN,
        GroupKind.STEP,
        GroupKind.STEP,
    ]
    assert result.entries[2].record_id == "tool"
    assert result.counts.lanes[TrajectoryLane.TOOLS] == 1
    assert result.counts.kinds[TrajectoryKind.TOOL_CALL] == 1
    assert result.counts.statuses[TrajectoryStatus.COMPLETED] == 1


def test_turn_and_step_collapse_independently() -> None:
    records = [record("one", "one", step_id="s1"), record("two", "two", step_id="s2")]
    groups = group_records(records)
    turn_id = groups[0].group_id
    step_id = groups[0].children[0].group_id

    turn = search_records(records, groups=groups, collapsed_groups={turn_id})
    assert [entry.group_id for entry in turn.entries] == [turn_id]

    step = search_records(records, groups=groups, collapsed_groups={step_id})
    assert [entry.group_id for entry in step.entries] == [
        turn_id,
        step_id,
        groups[0].children[1].group_id,
        groups[0].children[1].group_id,
    ]
    assert step.entries[1].collapsed
