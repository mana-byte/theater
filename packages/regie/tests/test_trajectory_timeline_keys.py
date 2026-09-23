from __future__ import annotations

from regie.trajectory.domain import TrajectoryRecord
from regie.trajectory.rich.widgets.timeline import Timeline
from textual.app import App, ComposeResult


def _record(record_id: str, lane: str, index: int) -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(
        {
            "record_id": record_id,
            "revision": 1,
            "participant_id": "p1",
            "source_epoch": "epoch",
            "lane": lane,
            "kind": "assistant" if lane == "model" else "tool_call",
            "source": "claude",
            "summary": record_id,
            "status": "completed",
            "raw_index": index,
        }
    )


class _Host(App):
    def compose(self) -> ComposeResult:
        yield Timeline(id="timeline")


async def test_lane_moves_reach_the_nearest_span_in_the_next_populated_lane() -> None:
    records = [
        _record("m1", "model", 1),
        _record("t2", "tools", 2),
        _record("m3", "model", 3),
        _record("m4", "model", 4),
        _record("t5", "tools", 5),
    ]
    async with _Host().run_test(size=(120, 30)) as pilot:
        timeline = pilot.app.query_one(Timeline)
        timeline.update_records(records, selected_id="m4")
        await pilot.pause()

        assert timeline.move_lane(1) == "t5"  # tools is below model; t5 is nearest to m4
        assert timeline.move_lane(1) == "t5"  # nothing populated further down
        assert timeline.move_lane(-1) == "m4"
        assert timeline.move_span(-1) == "m3"
