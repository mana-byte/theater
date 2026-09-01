from __future__ import annotations

from theater.regie.trajectory.enums import DiagnosticView
from theater.regie.trajectory.projection import TrajectoryViewProjection
from theater.regie.trajectory.render.pagination import paginate_search_result
from theater.regie.trajectory.search import (
    TrajectoryFilters,
    fuzzy_subsequence_score,
    record_search_text,
    search_records,
)
from theater.regie.trajectory.state import ParticipantTrajectoryState
from theater.trajectory import (
    GroupKind,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectorySearchResult,
    TrajectoryStatus,
    group_records,
    ranked_records,
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


def test_search_uses_mcp_identity() -> None:
    item = TrajectoryRecord.from_wire(
        {
            **record("mcp-call", "unrelated").to_wire(),
            "mcp_server": "grafana",
            "mcp_tool": "query_prometheus",
        }
    )

    assert search_records([item], query="prometheus").record_ids == ("mcp-call",)


def test_search_tolerates_typo_and_ranks_structural_identity_first() -> None:
    structural = TrajectoryRecord.from_wire(
        {
            **record("structural", "query metrics").to_wire(),
            "mcp_server": "grafana",
            "mcp_tool": "query_prometheus",
        }
    )
    incidental = record("incidental", "grafana appeared in a long result")

    ranked = ranked_records((incidental, structural), "grafna")

    assert [item.record_id for item, _score in ranked] == ["structural", "incidental"]
    assert search_records((structural,), query="grafna prometheus").record_ids == ("structural",)
    assert not search_records((structural,), query="grafna loki").record_ids


def test_full_history_hits_remain_searchable_in_tools_view() -> None:
    item = TrajectoryRecord.from_wire(
        {
            **record("remote-tool", "query metrics").to_wire(),
            "lane": "tools",
            "kind": "tool_call",
            "mcp_server": "grafana",
            "mcp_tool": "query_prometheus",
        }
    )
    state = ParticipantTrajectoryState("p1")
    state.query = "grafna"
    state.apply_search(TrajectorySearchResult(query="grafna", records=(item,)))
    state.diagnostic_view = DiagnosticView.TOOLS

    projection = TrajectoryViewProjection(state, page_size=30)
    projection.refresh(state, page_size=30)

    assert projection.search_result.record_ids == ("remote-tool",)


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
        GroupKind.STEP,
        GroupKind.STEP,
    ]
    assert result.entries[1].record_id == "tool"
    assert result.counts.lanes[TrajectoryLane.TOOLS] == 1
    assert result.counts.kinds[TrajectoryKind.TOOL_CALL] == 1
    assert result.counts.statuses[TrajectoryStatus.COMPLETED] == 1


def test_turn_headers_are_hidden_and_step_groups_stay_expanded() -> None:
    records = [record("one", "one", step_id="s1"), record("two", "two", step_id="s2")]
    groups = group_records(records)
    turn_id = groups[0].group_id
    step_id = groups[0].children[0].group_id

    result = search_records(records, groups=groups)
    assert [entry.group_id for entry in result.entries] == [
        step_id,
        step_id,
        groups[0].children[1].group_id,
        groups[0].children[1].group_id,
    ]
    assert turn_id not in [entry.group_id for entry in result.entries]


def test_between_turn_headers_are_hidden() -> None:
    records = [record("between", "between", turn_id=None)]
    groups = group_records(records)

    result = search_records(records, groups=groups)

    assert groups[0].kind is GroupKind.BETWEEN_TURNS
    assert len(result.entries) == 1
    assert result.entries[0].record_id == "between"
    assert not result.entries[0].is_header


def test_pagination_counts_records_and_repeats_needed_step_headers() -> None:
    records = [record("one", "one", step_id="s1"), record("two", "two", step_id="s1")]
    result = search_records(records, groups=group_records(records))

    first = paginate_search_result(result, 0, 1)
    second = paginate_search_result(result, 1, 1)

    assert (first.number, first.count, first.first_item, first.last_item) == (1, 2, 1, 1)
    assert first.record_ids == ("one",)
    assert second.record_ids == ("two",)
    assert [entry.group_kind for entry in first.result.entries] == [
        GroupKind.STEP,
        GroupKind.STEP,
    ]
    assert [entry.group_kind for entry in second.result.entries] == [
        GroupKind.STEP,
        GroupKind.STEP,
    ]
