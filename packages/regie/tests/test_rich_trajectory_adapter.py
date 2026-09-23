from __future__ import annotations

import asyncio
import json
from types import MappingProxyType, SimpleNamespace
from typing import cast

import pytest
from regie.trajectory.adapter import TrajectoryFollowAdapter, TrajectoryQueryAdapter
from regie.trajectory.domain import (
    ContentFormat,
    DetailField,
    PanelState,
    PanelStateInfo,
    TrajectoryDelta,
    TrajectoryPage,
    TrajectoryRecord,
    TrajectoryValidationError,
)
from regie.trajectory.rich.models import decode_delta, decode_page
from regie.trajectory.rich.state import ParticipantTrajectoryState, TrajectoryStateStore

from theater.frontend import FrontendClient


def wire_record(
    record_id: str,
    *,
    revision: int = 1,
    participant_id: str = "p1",
    lane: str = "model",
    kind: str = "assistant",
    summary: str = "summary",
    turn_id: str | None = "turn-1",
    step_id: str | None = None,
    details: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "record_id": record_id,
        "revision": revision,
        "participant_id": participant_id,
        "source_epoch": "epoch-1",
        "lane": lane,
        "kind": kind,
        "source": "claude",
        "summary": summary,
        "status": "completed",
    }
    if turn_id is not None:
        result["turn_id"] = turn_id
    if step_id is not None:
        result["step_id"] = step_id
    if details is not None:
        result["details"] = details
    return result


def page_wire(records: list[dict[str, object]], *, stream_id: str = "stream") -> dict[str, object]:
    return {
        "panel_state": {"state": "ready", "participant_state": "live"},
        "stream_id": stream_id,
        "cursor": "cursor-1",
        "records": records,
        "groups": [],
        "older_cursor": None,
        "has_older": False,
    }


def test_adapter_uses_canonical_page_and_delta_without_participant_fields() -> None:
    page = decode_page(page_wire([wire_record("r1")]))
    delta = decode_delta(
        {
            "stream_id": "stream",
            "cursor": "cursor-2",
            "upserts": [{"record": wire_record("r2")}],
        }
    )

    assert isinstance(page, TrajectoryPage)
    assert isinstance(delta, TrajectoryDelta)
    assert page.panel_state.state is PanelState.READY
    assert delta.upserts[0].record.record_id == "r2"
    with pytest.raises(TrajectoryValidationError):
        decode_page(page_wire([wire_record("r1")]) | {"participant_id": "p1"})


def test_canonical_detail_format_and_literal_content_are_preserved() -> None:
    record = TrajectoryRecord.from_wire(
        wire_record(
            "r1",
            summary="[literal] \\ path",
            details=[
                {
                    "name": "payload",
                    "format": "json",
                    "value": {"text": '{"b": 2, "a": 1}', "omitted_bytes": 0},
                }
            ],
        )
    )

    assert record.summary == "[literal] \\ path"
    assert record.details[0].format is ContentFormat.JSON
    assert record.details[0].preview.text.startswith("{")
    assert DetailField.from_text("x", "y").preview.text == "y"


def test_runtime_state_rejects_mixed_participant_and_keeps_revision_precedence() -> None:
    state = TrajectoryStateStore().get("p1")
    first = TrajectoryRecord.from_wire(wire_record("r1"))
    newer = TrajectoryRecord.from_wire(wire_record("r1", revision=2, summary="new"))
    other = TrajectoryRecord.from_wire(wire_record("other", participant_id="p2"))

    assert state.upsert([first]) == (1, 0)
    assert state.upsert([newer]) == (0, 1)
    assert state.records["r1"].summary == "new"
    with pytest.raises(TrajectoryValidationError):
        state.upsert([other])


def test_runtime_state_skips_rebuilding_indexes_for_unchanged_upserts(monkeypatch) -> None:
    state = TrajectoryStateStore().get("p1")
    item = TrajectoryRecord.from_wire(wire_record("r1"))
    state.upsert([item])
    rebuilds = 0
    original_rebuild = ParticipantTrajectoryState._rebuild_groups

    def count_rebuild(self: ParticipantTrajectoryState) -> None:
        nonlocal rebuilds
        rebuilds += 1
        original_rebuild(self)

    monkeypatch.setattr(ParticipantTrajectoryState, "_rebuild_groups", count_rebuild)

    assert state.upsert([item]) == (0, 0)
    assert rebuilds == 0


def test_runtime_state_repairs_tail_selection_for_unchanged_upserts() -> None:
    state = TrajectoryStateStore().get("p1")
    item = TrajectoryRecord.from_wire(wire_record("r1"))
    state.upsert([item])
    state.selected_id = None

    assert state.upsert([item]) == (0, 0)
    assert state.selected_id == "r1"


def test_empty_snapshot_rebuilds_indexes_after_replacing_loaded_records() -> None:
    state = TrajectoryStateStore().get("p1")
    item = TrajectoryRecord.from_wire(wire_record("r1", kind="tool_call"))
    state.upsert([item])
    assert state.groups
    assert state.tool_index.ordered

    state.apply_snapshot(TrajectoryPage(PanelStateInfo(PanelState.READY), records=()))

    assert not state.records
    assert not state.groups
    assert not state.request_index.by_id
    assert not state.tool_index.ordered


def test_runtime_state_counts_compact_utf8_wire_bytes() -> None:
    state = TrajectoryStateStore().get("p1")
    item = TrajectoryRecord.from_wire(wire_record("r1", summary="régie"))

    state.upsert([item])

    encoded = json.dumps(item.to_wire(), ensure_ascii=False, separators=(",", ":")).encode()
    assert state.loaded_bytes == len(encoded)


def test_state_store_applies_configured_page_size() -> None:
    store = TrajectoryStateStore(page_size=17)

    assert store.get("p1").participant_id == "p1"
    assert store.get("p2").participant_id == "p2"
    assert store.page_size == 17


@pytest.mark.asyncio
async def test_public_query_adapter_serializes_its_interactive_connection() -> None:
    class Trajectory:
        def __init__(self) -> None:
            self.active = 0
            self.maximum_active = 0

        async def snapshot(self, participant_id: str, *, limit: int) -> object:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            await asyncio.sleep(0)
            self.active -= 1
            return SimpleNamespace(value={"participant_id": participant_id})

    client = SimpleNamespace(trajectory=Trajectory())
    adapter = TrajectoryQueryAdapter(cast(FrontendClient, client))

    await asyncio.gather(
        adapter.call("trajectory.snapshot", id="p1", limit=30),
        adapter.call("trajectory.snapshot", id="p2", limit=30),
    )

    assert client.trajectory.maximum_active == 1


@pytest.mark.asyncio
async def test_public_adapters_thaw_the_sdk_immutable_json_view() -> None:
    record = MappingProxyType(
        {
            **wire_record("r1"),
            "links": (),
            "details": (
                MappingProxyType(
                    {
                        "name": "payload",
                        "format": "text",
                        "value": MappingProxyType({"text": "hello", "omitted_bytes": 0}),
                    }
                ),
            ),
        }
    )

    class Trajectory:
        async def snapshot(self, participant_id: str, *, limit: int) -> object:
            assert participant_id == "p1"
            assert limit == 200
            return SimpleNamespace(
                value=MappingProxyType(
                    {
                        **page_wire([]),
                        "records": (record,),
                        "groups": (),
                    }
                )
            )

        async def follow(
            self,
            stream_id: str,
            cursor: str,
            *,
            wait_seconds: int,
        ) -> object:
            assert (stream_id, cursor, wait_seconds) == ("stream", "cursor-1", 20)
            return SimpleNamespace(
                value=MappingProxyType(
                    {
                        "stream_id": "stream",
                        "cursor": "cursor-2",
                        "upserts": (MappingProxyType({"record": record}),),
                    }
                )
            )

    client = SimpleNamespace(trajectory=Trajectory())
    query = TrajectoryQueryAdapter(cast(FrontendClient, client))
    follow = TrajectoryFollowAdapter(cast(FrontendClient, client))

    page = decode_page(await query.call("trajectory.snapshot", id="p1", limit=200))
    delta = decode_delta(
        await follow.call(
            "trajectory.follow",
            stream_id="stream",
            after="cursor-1",
            wait=20,
        )
    )

    assert page.records[0].details[0].preview.text == "hello"
    assert delta.upserts[0].record.record_id == "r1"
