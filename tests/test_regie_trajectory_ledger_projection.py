"""Pure ledger row projection coverage."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest
from rich.console import Console
from rich.style import Style

from theater.regie.trajectory.render.ledger import (
    COLUMN_LABELS,
    LedgerRowValues,
    LedgerStylePalette,
    empty_values,
    group_values,
    history_values,
    record_cells,
    record_values,
    request_values,
    retry_values,
    tool_values,
)
from theater.regie.trajectory.render.requests import build_request_index
from theater.regie.trajectory.render.tools import build_tool_index
from theater.regie.trajectory.widgets.ledger import Ledger
from theater.trajectory import (
    DetailField,
    GroupKind,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryRecord,
    TrajectoryStatus,
    TrajectoryUsage,
)


def _record(
    record_id: str = "record",
    *,
    lane: TrajectoryLane = TrajectoryLane.MODEL,
    kind: TrajectoryKind = TrajectoryKind.ASSISTANT,
    source: str = "adapter",
    summary: str = "summary",
    status: TrajectoryStatus = TrajectoryStatus.COMPLETED,
    **kwargs: object,
) -> TrajectoryRecord:
    return TrajectoryRecord(
        record_id=record_id,
        revision=1,
        participant_id="participant",
        source_epoch="epoch",
        lane=lane,
        kind=kind,
        source=source,
        summary=summary,
        status=status,
        **kwargs,
    )


def _palette() -> LedgerStylePalette:
    return LedgerStylePalette(
        input=Style(color="blue"),
        model=Style(color="cyan"),
        tools=Style(color="yellow"),
        theater=Style(color="magenta"),
        muted=Style(color="white", dim=True),
        warning=Style(color="yellow"),
        error=Style(color="red"),
        accent=Style(color="cyan", dim=True),
        retry=Style(color="white", bold=True),
        request=Style(color="cyan", dim=True),
    )


def test_record_values_are_immutable_and_compact_projection_is_canonical() -> None:
    record = _record(summary="first\nsecond")

    values = record_values(record, 2, depth=1, hovered=True, compact=True)

    assert values == LedgerRowValues(
        position="●  3",
        event="◆ ASSISTANT",
        source="adapter",
        summary="[adapter]   first\nsecond",
        status="● completed",
        duration="—",
    )
    assert dict(values)["summary"] == values.summary
    assert COLUMN_LABELS["event"] == "EVENT"
    with pytest.raises(FrozenInstanceError):
        values.summary = "changed"  # type: ignore[misc]


def test_rich_cells_use_the_canonical_values_and_explicit_palette() -> None:
    record = _record()
    palette = _palette()
    values = record_values(record, 0, depth=0, hovered=True, compact=False)
    cells = record_cells(record, values, palette, hovered=True, duration_mode=False)

    assert {key: cell.plain for key, cell in cells.items()} == dict(values)
    assert cells["event"].get_style_at_offset(Console(), 0).color == palette.model.color
    assert cells["status"].get_style_at_offset(Console(), 0).color == palette.muted.color
    assert cells["summary"].get_style_at_offset(Console(), 0).bold
    assert isinstance(Ledger.COLUMN_LABELS, dict)
    assert Ledger.COLUMN_LABELS == COLUMN_LABELS


def test_tool_request_group_and_auxiliary_rows_keep_plain_text() -> None:
    request_record = _record(
        "request-record",
        usage=TrajectoryUsage(model="model-x", input_tokens=3, output_tokens=2),
        request_id="source-request",
    )
    request = build_request_index((request_record,)).ordered[0]
    tool_record = replace(
        _record("tool-record", lane=TrajectoryLane.TOOLS, kind=TrajectoryKind.TOOL_CALL),
        call_id="call",
        details=(DetailField.from_text("tool", "runner"),),
        status=TrajectoryStatus.RUNNING,
    )
    tool = build_tool_index((tool_record,)).ordered[0]

    assert tool_values(tool, 0, depth=2, hovered=False, compact=False) == LedgerRowValues(
        position="▸  1",
        event="⚙ TOOL",
        source="adapter",
        summary="    [runner] awaiting result",
        status="● running",
        duration="—",
    )
    assert request_values(request, depth=1, compact=False).summary.startswith("  in 3 · out 2")
    assert request_values(None, depth=0, compact=False) == LedgerRowValues(
        position="↗",
        event="◆ REQUEST",
        source="model unknown",
        summary="usage unavailable",
        status="● unknown",
        duration="—",
    )
    assert group_values(GroupKind.TURN, "Turn", depth=1) == LedgerRowValues(
        event="TURN", summary="  Turn"
    )
    assert history_values(loading=False).summary == "Load earlier events"
    assert history_values(loading=True).position == "…"
    assert empty_values().event == "EMPTY"
    assert retry_values("try\nagain").summary == "try again"
