"""Focused logical tool-row coverage for the Régie trajectory ledger."""

from __future__ import annotations

from types import MappingProxyType

from regie.trajectory.domain import (
    DetailField,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryRequest,
    TrajectoryRequestIdentity,
    TrajectoryStatus,
)
from regie.trajectory.domain.records import TrajectoryFailure
from regie.trajectory.rich.render.requests import RequestIndex
from regie.trajectory.rich.render.tools import build_tool_index
from regie.trajectory.rich.state import TrajectoryStateStore
from regie.trajectory.rich.view import TrajectoryView
from textual.app import App, ComposeResult


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
