"""Codex raw and item rollout streams collapse to one logical call."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.codex_rollout_items import (
    file_change_item,
    item_completed,
    mcp_item,
    parse_rollout,
    rollout_events,
    rollout_facts,
    rollout_record,
    session_meta,
)
from theater.daemon.trajectory.project import project_events_and_facts
from theater.harness.base import EventKind
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.provenance import TranscriptProvenance
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus


def _canonical(parsed):
    return project_events_and_facts(
        [
            event
            for record in parsed
            for event in (
                record.trajectory_events if record.trajectory_events is not None else record.events
            )
        ],
        rollout_facts(parsed),
        participant_id="p",
        source_epoch="e",
    )


def _raw_tool_call(
    call_id: str,
    *,
    name: str = "apply_patch",
    input_text: str = "*** Begin Patch\n*** Add File: theater/new.py\n+ x = 1\n*** End Patch",
) -> dict:
    return rollout_record(
        "response_item",
        {
            "type": "custom_tool_call",
            "id": f"ctc-{call_id}",
            "call_id": call_id,
            "name": name,
            "input": input_text,
        },
    )


def _raw_tool_output(call_id: str, *, output: str = "ok", record_id: str | None = None) -> dict:
    return rollout_record(
        "response_item",
        {
            "type": "custom_tool_call_output",
            "id": record_id or f"ctco-{call_id}",
            "call_id": call_id,
            "output": output,
        },
    )


def _dual_stream_records(
    call_id: str,
    item: dict,
    *,
    output: str = "ok",
    output_record_id: str | None = None,
    item_before_output: bool = True,
    name: str = "apply_patch",
) -> list[dict]:
    """One logical tool call persisted as raw records *and* a rich item.

    Native Codex sets ``item.id = raw call_id``
    (``core/src/tools/events.rs`` ``emit_patch_end``,
    ``core/src/mcp_tool_call.rs``), and ``rollout/src/policy.rs`` persists the
    raw CustomToolCall/Output pair, so a paginated rollout can carry both
    representations of the same call. The native order is call → item →
    output (``emit_turn_item_completed`` runs before the call result lands).
    """
    item_record = item_completed(
        "turn-a", item, started_ms=1789040005000, completed_ms=1789040007000
    )
    records = [_raw_tool_call(call_id, name=name)]
    if item_before_output:
        records.append(item_record)
        records.append(_raw_tool_output(call_id, output=output, record_id=output_record_id))
    else:
        records.append(_raw_tool_output(call_id, output=output, record_id=output_record_id))
        records.append(item_record)
    return records


def test_raw_patch_and_item_collapse_to_one_logical_call():
    """Probe: raw call + correlated FileChange item + raw output = ONE pair.

    Before the correlation the canonical store held two tool calls
    (``e:call-1`` and ``e:call-1:call``) and two tool results
    (``e:call-1:result`` and a coordinate fallback), and control saw two
    TOOL_CALL events. The item's ``id`` *is* the raw ``call_id``, so the
    item adopts the raw identity and a revision above the raw facts; the
    canonical merge keeps one call and one result, carrying the item's
    rich detail, and the raw pair keeps the single control event pair.
    """
    observer = CodexObserver()
    records = [
        session_meta(),
        *_dual_stream_records("call-1", file_change_item("call-1")),
    ]
    parsed = parse_rollout(observer, records)

    calls = [e for e in rollout_events(parsed) if e.kind is EventKind.TOOL_CALL]
    results = [e for e in rollout_events(parsed) if e.kind is EventKind.TOOL_RESULT]
    assert len(calls) == 1, "the raw pair is the only control source"
    assert len(results) == 1
    assert calls[0].tool_name == "apply_patch"
    assert results[0].text == "ok"

    canonical = _canonical(parsed)
    # One logical call: the item adopted the raw call's native id (they differ
    # in the raw id field — only the call_id correlation is relied on), and
    # the result claims the scoped id the item first announced.
    assert sorted(
        (r.kind, r.record_id) for r in canonical if r.kind is TrajectoryKind.TOOL_CALL
    ) == [(TrajectoryKind.TOOL_CALL, "e:ctc-call-1")]
    assert sorted(
        (r.kind, r.record_id) for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT
    ) == [(TrajectoryKind.TOOL_RESULT, "e:call-1:result")]
    call_record = next(r for r in canonical if r.kind is TrajectoryKind.TOOL_CALL)
    result_record = next(r for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT)
    # The surviving records are the item's rich versions, not the raw pair's.
    assert call_record.timing is not None
    assert call_record.timing.start == pytest.approx(1789040005.0)
    assert result_record.summary == "patch applied"
    assert result_record.status is TrajectoryStatus.COMPLETED
    assert result_record.failure is None
    # The rich paths survive on the fact that fed the canonical call record.
    call_fact = next(f for f in rollout_facts(parsed) if f.kind is TrajectoryKind.TOOL_CALL)
    assert call_fact.native_id == "ctc-call-1"
    assert "theater/new.py" in json.dumps(
        [getattr(detail, "value", None) for detail in call_fact.details], default=str
    )


def test_raw_mcp_and_item_collapse_to_one_logical_call():
    observer = CodexObserver()
    records = [
        session_meta(),
        *_dual_stream_records(
            "mcp-1",
            mcp_item("mcp-1"),
            name="mcp__theater__whoami",
        ),
    ]
    parsed = parse_rollout(observer, records)

    calls = [e for e in rollout_events(parsed) if e.kind is EventKind.TOOL_CALL]
    results = [e for e in rollout_events(parsed) if e.kind is EventKind.TOOL_RESULT]
    assert len(calls) == 1
    assert len(results) == 1

    canonical = _canonical(parsed)
    call_records = [r for r in canonical if r.kind is TrajectoryKind.THEATER_CALL]
    result_records = [r for r in canonical if r.kind is TrajectoryKind.THEATER_RESULT]
    assert [r.record_id for r in call_records] == ["e:ctc-mcp-1"]
    assert [r.record_id for r in result_records] == ["e:mcp-1:result"]
    # MCP identity and the item's result text survive the merge.
    assert call_records[0].mcp_server == "theater"
    assert call_records[0].mcp_tool == "whoami"
    assert result_records[0].summary == "whoami completed"
    assert result_records[0].status is TrajectoryStatus.COMPLETED


def test_dual_stream_failure_path_retains_the_error():
    """A failed call keeps exactly one record pair — with its failure."""
    observer = CodexObserver()
    item = file_change_item("call-1", status="failed")
    item["stderr"] = "patch conflict"
    records = [
        session_meta(),
        *_dual_stream_records("call-1", item, output="patch failed"),
    ]
    parsed = parse_rollout(observer, records)
    canonical = _canonical(parsed)
    result_records = [r for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT]
    assert [r.record_id for r in result_records] == ["e:call-1:result"]
    assert result_records[0].status is TrajectoryStatus.ERROR
    assert result_records[0].failure is not None
    assert result_records[0].failure.category is not None


def test_dual_stream_output_before_item_collapses_too():
    """Order-independence: call, output, item still yields one pair."""
    observer = CodexObserver()
    records = [
        session_meta(),
        *_dual_stream_records(
            "call-1",
            file_change_item("call-1"),
            output_record_id="ctco-1",
            item_before_output=False,
        ),
    ]
    parsed = parse_rollout(observer, records)
    canonical = _canonical(parsed)
    assert sorted(
        (r.kind, r.record_id) for r in canonical if r.kind is TrajectoryKind.TOOL_CALL
    ) == [(TrajectoryKind.TOOL_CALL, "e:ctc-call-1")]
    assert sorted(
        (r.kind, r.record_id) for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT
    ) == [(TrajectoryKind.TOOL_RESULT, "e:ctco-1")]
    calls = [e for e in rollout_events(parsed) if e.kind is EventKind.TOOL_CALL]
    results = [e for e in rollout_events(parsed) if e.kind is EventKind.TOOL_RESULT]
    assert len(calls) == 1
    assert len(results) == 1
    # The surviving result is the item's rich version.
    result_record = next(r for r in canonical if r.kind is TrajectoryKind.TOOL_RESULT)
    assert result_record.summary == "patch applied"


@pytest.mark.asyncio
async def test_dual_stream_patch_and_mcp_agree_between_history_and_live(tmp_path: Path):
    """The correlation holds on both ingestion paths, cold and live."""
    path = tmp_path / "rollout.jsonl"

    def write(records):
        with path.open("a") as fh:
            for record in records:
                fh.write(json.dumps(record) + "\n")

    def turn_records(turn_id: str) -> list[dict]:
        patch = _dual_stream_records(f"{turn_id}-patch", file_change_item(f"{turn_id}-patch"))
        mcp = _dual_stream_records(f"{turn_id}-mcp", mcp_item(f"{turn_id}-mcp"))
        return [
            rollout_record("event_msg", {"type": "task_started", "turn_id": turn_id}),
            *patch,
            *mcp,
            rollout_record("event_msg", {"type": "task_complete", "turn_id": turn_id}),
        ]

    write([session_meta(), *turn_records("turn-a")])

    def source():
        observer = CodexObserver(root=tmp_path)
        return observer.open_source(
            cwd=str(tmp_path),
            known_location=str(path),
            session_provenance=TranscriptProvenance.OPERATOR,
        )

    cold_source = source()
    cold_page = await cold_source.history_page(limit=50)
    cold_canonical = project_events_and_facts(
        list(cold_page.events),
        cold_page.trajectory,
        participant_id="p",
        source_epoch="e",
    )

    live_source = source()
    attached = await live_source.read()
    assert attached.attached is not None
    live_source.commit_attachment()
    write(turn_records("turn-b"))
    batch = await live_source.read()
    live_canonical = project_events_and_facts(
        list(batch.events),
        batch.trajectory,
        participant_id="p",
        source_epoch="e",
    )

    def shape(canonical, turn_id):
        return sorted(
            (r.kind, r.record_id.replace(turn_id, "turn"), r.summary)
            for r in canonical
            if r.kind
            in (
                TrajectoryKind.TOOL_CALL,
                TrajectoryKind.TOOL_RESULT,
                TrajectoryKind.THEATER_CALL,
                TrajectoryKind.THEATER_RESULT,
            )
        )

    assert shape(cold_canonical, "turn-a") == shape(live_canonical, "turn-b")
    # Exactly one call/result pair per tool on both paths.
    assert len(shape(cold_canonical, "turn-a")) == 4
    # Control agrees: one event pair per tool.
    live_calls = [e for e in batch.events if e.kind is EventKind.TOOL_CALL]
    live_results = [e for e in batch.events if e.kind is EventKind.TOOL_RESULT]
    assert len(live_calls) == 2
    assert len(live_results) == 2
