from __future__ import annotations

from dataclasses import replace

import pytest
import regie.trajectory.rich.state as state_module
from regie.trajectory.domain import (
    PanelState,
    PanelStateInfo,
    Timing,
    TimingProvenance,
    TrajectoryDelta,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUpsert,
    TrajectoryUsage,
)
from regie.trajectory.domain.enums import CostProvenance, TrajectoryFailureCategory
from regie.trajectory.domain.records import TrajectoryFailure
from regie.trajectory.rich.enums import InspectorTab
from regie.trajectory.rich.inspection.lines import (
    request_association_lines,
    request_timing_lines,
    request_usage_lines,
)
from regie.trajectory.rich.inspection.styled import build_span_details
from regie.trajectory.rich.render.requests import build_request_index
from regie.trajectory.rich.state import ParticipantTrajectoryState


def record(
    record_id: str,
    index: int,
    *,
    request_id: str | None = None,
    usage: TrajectoryUsage | None = None,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    revision: int = 1,
    turn_id: str | None = None,
    step_id: str | None = None,
    timing: Timing | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id="p1",
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="adapter",
        summary=record_id,
        status=status,
        raw_index=index,
        request_id=request_id,
        usage=usage,
        turn_id=turn_id,
        step_id=step_id,
        timing=timing,
    )


def test_request_index_is_immutable_and_state_keeps_the_final_retained_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = record("first", 1, request_id="shared")
    second = record("second", 2, request_id="shared")
    usage = record("usage", 3, usage=TrajectoryUsage(request_id="usage-request"))
    index = build_request_index((first, second, usage))

    shared = index.by_record_id["first"]
    assert index.by_record_id == {
        "first": shared,
        "second": shared,
        "usage": index.by_record_id["usage"],
    }
    assert index.by_id[shared].record_ids == ("first", "second")
    assert index.by_id[index.by_record_id["usage"]].source_request_id == "usage-request"

    monkeypatch.setattr(state_module, "TRAJECTORY_UI_RECORD_LIMIT", 1)
    monkeypatch.setattr(state_module, "TRAJECTORY_UI_MAX_BYTES", 1_000_000)
    state = ParticipantTrajectoryState("p1")
    state.upsert((first, second))
    assert tuple(state.records) == ("second",)
    assert state.request_index.by_record_id == {"second": shared}

    state.apply_snapshot(
        TrajectoryPage(
            PanelStateInfo(PanelState.READY),
            stream_id="stream",
            records=(first,),
        )
    )
    updated = replace(first, revision=2, status=TrajectoryStatus.RUNNING)
    state.apply_follow(TrajectoryDelta("stream", upserts=(TrajectoryUpsert(updated),)))
    prior_index = state.request_index
    assert prior_index.by_id[shared].status is TrajectoryStatus.RUNNING

    state.apply_snapshot(TrajectoryPage(PanelStateInfo(PanelState.STALE), stream_id="stream"))
    assert state.request_index is prior_index
    assert tuple(state.records) == ("first",)


def test_request_inspector_exposes_diagnostics_and_exact_associations() -> None:
    context = replace(
        record("context", 1, request_id="request"),
        kind=TrajectoryKind.CONTEXT,
    )
    model = replace(
        record(
            "model",
            2,
            request_id="request",
            usage=TrajectoryUsage(
                model="model-x",
                provider="provider-x",
                output_tokens=100,
                cost_usd=0.25,
                cost_provenance=CostProvenance.REPORTED,
            ),
            timing=Timing(
                start=10.0,
                first_token=10.2,
                end=11.2,
                provenance=TimingProvenance.SOURCE,
            ),
        ),
        status=TrajectoryStatus.ERROR,
        failure=TrajectoryFailure(
            TrajectoryFailureCategory.PROVIDER,
            code="rate_limit",
            detail="retry later",
        ),
        retry_of_record_id="prior",
        retry_attempt=2,
    )
    tool = replace(
        record("tool", 3, request_id="request"),
        lane=TrajectoryLane.TOOLS,
        kind=TrajectoryKind.TOOL_CALL,
    )
    coordination = replace(
        record("coordination", 4, request_id="request"),
        lane=TrajectoryLane.THEATER,
        kind=TrajectoryKind.SEND,
    )
    request = build_request_index((context, model, tool, coordination)).ordered[0]

    usage = "\n".join(line.text for line in request_usage_lines(request))
    timing = "\n".join(line.text for line in request_timing_lines(request))
    associations = request_association_lines(request)
    details = build_span_details(
        model,
        InspectorTab.ASSOCIATIONS,
        request=request,
    )

    assert "Provider: provider-x" in usage
    assert "Cost: $0.25 · reported" in usage
    assert "Time to first token: 200ms" in timing
    assert "Generation duration: 1s" in timing
    assert "Output throughput: 100.00 tok/s" in timing
    assert {line.target_record_id for line in associations if line.target_record_id} == {
        "context",
        "model",
        "tool",
        "coordination",
        "prior",
    }
    assert "Retry of: prior · attempt 2" in details.copy_text
    assert InspectorTab.ASSOCIATIONS in details.tabs


def test_accounting_follow_update_does_not_announce_new_activity() -> None:
    answer = record("answer", 1, request_id="request")
    accounting = replace(
        record(
            "accounting",
            2,
            request_id="request",
            usage=TrajectoryUsage(input_tokens=42),
        ),
        kind=TrajectoryKind.USAGE,
        summary="",
    )
    state = ParticipantTrajectoryState("p1")
    state.apply_snapshot(
        TrajectoryPage(
            PanelStateInfo(PanelState.READY),
            stream_id="stream",
            records=(answer,),
        )
    )
    state.pause_follow()

    assert state.apply_follow(
        TrajectoryDelta("stream", upserts=(TrajectoryUpsert(accounting),))
    ) == (1, 0)
    assert state.new_count == 0
    assert state.selected_id == "answer"
    assert state.request_index.ordered[0].usage == accounting.usage
