"""Trajectory state: overview facts, the request index and tool-row ledger."""

from __future__ import annotations

from dataclasses import replace
from types import MappingProxyType

import pytest
import regie.trajectory.rich.state as state_module
from regie.trajectory.rich.render.requests import RequestIndex, build_request_index
from regie.trajectory.rich.render.tools import build_tool_index
from regie.trajectory.rich.state import ParticipantTrajectoryState, TrajectoryStateStore
from regie.trajectory.rich.view import TrajectoryView
from textual.app import App, ComposeResult
from textual.widgets import Label

from theater.frontend.trajectory import (
    DetailField,
    PanelState,
    PanelStateInfo,
    Timing,
    TrajectoryCapabilities,
    TrajectoryCurrentOperation,
    TrajectoryDelta,
    TrajectoryFailure,
    TrajectoryFeature,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryOverview,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryRequestIdentity,
    TrajectoryStatus,
    TrajectoryUpsert,
    TrajectoryUsage,
)


def panel(
    state: PanelState = PanelState.READY, participant_state: str = "live", message: str = ""
) -> PanelStateInfo:
    return PanelStateInfo(state, message, participant_state=participant_state)


def current(
    *,
    start: float | None = None,
    duration_ms: float | None = 8_400,
    summary: str = "pytest tests/test_rpc.py",
) -> TrajectoryCurrentOperation:
    return TrajectoryCurrentOperation(
        record_id="r1",
        kind="tool_call",
        lane="tools",
        status="running",
        summary=summary,
        model="gpt-5.6",
        start=start,
        duration_ms=duration_ms,
    )


def text(label: Label) -> str:
    return str(label.render())


class OverviewHost(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("p1", id="trajectory", focus_on_mount=False)


def test_state_snapshot_older_and_follow_copy_backend_overview_facts() -> None:
    state = ParticipantTrajectoryState("p1")
    first_capabilities = TrajectoryCapabilities(supported=frozenset({TrajectoryFeature.TOOLS}))
    first_overview = TrajectoryOverview(record_count=2)
    state.apply_snapshot(
        TrajectoryPage(
            panel_state=panel(),
            stream_id="stream",
            capabilities=first_capabilities,
            overview=first_overview,
        )
    )
    assert state.capabilities is first_capabilities
    assert state.overview is first_overview

    older_capabilities = TrajectoryCapabilities(unsupported=frozenset({TrajectoryFeature.USAGE}))
    older_overview = TrajectoryOverview(record_count=4)
    state.apply_older(
        TrajectoryPage(
            panel_state=panel(), capabilities=older_capabilities, overview=older_overview
        )
    )
    assert state.capabilities is older_capabilities
    assert state.overview is older_overview

    follow_capabilities = TrajectoryCapabilities(observed=frozenset({TrajectoryFeature.TIMING}))
    follow_overview = TrajectoryOverview(record_count=5)
    state.apply_follow(
        TrajectoryDelta(
            stream_id="stream", capabilities=follow_capabilities, overview=follow_overview
        )
    )
    assert state.capabilities is follow_capabilities
    assert state.overview is follow_overview


def test_legacy_follow_preserves_backend_overview_facts() -> None:
    capabilities = TrajectoryCapabilities(supported=frozenset({TrajectoryFeature.MODELS}))
    overview = TrajectoryOverview(record_count=3)
    state = ParticipantTrajectoryState(
        "p1", stream_id="stream", capabilities=capabilities, overview=overview
    )

    state.apply_follow(TrajectoryDelta(stream_id="stream"))

    assert state.capabilities is capabilities
    assert state.overview is overview


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


def _tool(
    record_id: str,
    index: int,
    kind: TrajectoryKind,
    call_id: str | None,
    *,
    summary: str = "tool",
    details: tuple[DetailField, ...] = (),
    participant_id: str = "participant",
    source_epoch: str = "epoch",
    request_id: str | None = None,
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    revision: int = 1,
    turn_id: str | None = None,
    step_id: str | None = None,
    mcp_server: str | None = None,
    mcp_tool: str | None = None,
    failure: TrajectoryFailure | None = None,
    retry_of_record_id: str | None = None,
    retry_attempt: int | None = None,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=revision,
        participant_id=participant_id,
        source_epoch=source_epoch,
        lane=TrajectoryLane.TOOLS,
        kind=kind,
        source="codex",
        summary=summary,
        status=status,
        raw_index=index,
        call_id=call_id,
        request_id=request_id,
        turn_id=turn_id,
        step_id=step_id,
        mcp_server=mcp_server,
        mcp_tool=mcp_tool,
        details=details,
        failure=failure,
        retry_of_record_id=retry_of_record_id,
        retry_attempt=retry_attempt,
    )


def test_index_anchors_matched_operation_and_maps_every_member() -> None:
    call = _tool(
        "call", 1, TrajectoryKind.TOOL_CALL, "one", details=(DetailField.from_text("tool", "exec"),)
    )
    result = _tool("result", 2, TrajectoryKind.TOOL_RESULT, "one")

    index = build_tool_index((call, result))
    operation = index.ordered[0]

    assert index.anchor_by_id[operation.operation_id] == "call"
    assert index.by_record_id == {"call": operation.operation_id, "result": operation.operation_id}


def _ordinary(record_id: str, index: int) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=1,
        participant_id="participant",
        source_epoch="epoch",
        lane=TrajectoryLane.MODEL,
        kind=TrajectoryKind.ASSISTANT,
        source="codex",
        summary=record_id,
        status=TrajectoryStatus.COMPLETED,
        raw_index=index,
    )


def _request(
    request_id: str,
    source_request_id: str,
    *,
    participant_id: str = "participant",
    source_epoch: str = "epoch",
) -> TrajectoryRequest:
    return TrajectoryRequest(
        request_id=request_id,
        participant_id=participant_id,
        source_epoch=source_epoch,
        source="codex",
        record_ids=(f"member-{request_id}",),
        identity=TrajectoryRequestIdentity.SOURCE,
        source_request_id=source_request_id,
    )


def _request_index(
    *requests: TrajectoryRequest,
    direct: dict[str, str] | None = None,
) -> RequestIndex:
    return RequestIndex(
        ordered=requests,
        by_id=MappingProxyType({request.request_id: request for request in requests}),
        by_record_id=MappingProxyType(direct or {}),
    )


class _LedgerHost(App):
    def compose(self) -> ComposeResult:
        yield TrajectoryView("participant")


class _ViewHost(App):
    def __init__(self, state_store: TrajectoryStateStore) -> None:
        super().__init__()
        self.state_store = state_store

    def compose(self) -> ComposeResult:
        yield TrajectoryView("participant", state_store=self.state_store)
