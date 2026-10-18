from __future__ import annotations

import json
import stat
from datetime import UTC, datetime

import pytest
from regie.trajectory.rich.export import serialize_trajectory_export, write_trajectory_export
from regie.trajectory.ui_constants import TRAJECTORY_EXPORT_PAYLOAD_MAX_CHARS

from theater.frontend.trajectory import TrajectoryRecord


def _record(
    record_id: str,
    *,
    index: int,
    lane: str,
    kind: str,
    summary: str,
    payload: str,
) -> TrajectoryRecord:
    return TrajectoryRecord.from_wire(
        {
            "record_id": record_id,
            "revision": 1,
            "participant_id": "p1",
            "source_epoch": "epoch",
            "lane": lane,
            "kind": kind,
            "source": "claude",
            "summary": summary,
            "status": "completed",
            "raw_index": index,
            "timing": {
                "start": 1_700_000_000 + index,
                "duration_ms": index * 100,
                "provenance": "source",
            },
            "details": [
                {
                    "name": "input" if lane == "input" else "output",
                    "format": "text",
                    "value": {"text": payload, "omitted_bytes": 0},
                }
            ],
        }
    )


def test_export_serialization_round_trips_and_writes_ordered_bounded_markdown() -> None:
    huge = "x" * (TRAJECTORY_EXPORT_PAYLOAD_MAX_CHARS + 12)
    first = _record(
        "r1", index=1, lane="input", kind="user", summary="first summary", payload="prompt"
    )
    second = _record(
        "r2", index=2, lane="model", kind="assistant", summary="second summary", payload=huge
    )

    json_text, markdown = serialize_trajectory_export(
        (second, first),
        participant_id="p1",
        participant_name="worker",
        participant_harness="claude",
        exported_at=datetime(2026, 9, 25, 12, 30, tzinfo=UTC),
        filter_query="summary",
    )

    document = json.loads(json_text)
    decoded = tuple(TrajectoryRecord.from_wire(record) for record in document["records"])
    assert decoded == (first, second)
    assert document["filter"] == "summary"
    first_header = "### input · user · completed"
    second_header = "### model · assistant · completed"
    assert markdown.index(first_header) < markdown.index(second_header)
    assert "first summary" in markdown and "#### Prompt\n\n```\nprompt\n```" in markdown
    assert "… [truncated: 12 characters omitted]" in markdown


def test_write_export_uses_private_collision_free_file_pairs(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("regie.trajectory.rich.export.trajectory_export_dir", lambda: tmp_path)
    record = _record("r1", index=1, lane="input", kind="user", summary="summary", payload="prompt")
    instant = datetime(2026, 9, 25, 12, 30, tzinfo=UTC)

    first = write_trajectory_export(
        (record,),
        participant_id="p1",
        participant_name="worker",
        participant_harness="claude",
        filter_query=None,
        exported_at=instant,
    )
    second = write_trajectory_export(
        (record,),
        participant_id="p1",
        participant_name="worker",
        participant_harness="claude",
        filter_query=None,
        exported_at=instant,
    )

    assert first.json_path.stem == first.markdown_path.stem
    assert second.json_path.stem == second.markdown_path.stem
    assert first.json_path != second.json_path
    paths = (first.json_path, first.markdown_path, second.json_path, second.markdown_path)
    assert all(path.is_file() for path in paths)
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    for path in paths:
        assert stat.S_IMODE(path.stat().st_mode) & ~0o600 == 0
