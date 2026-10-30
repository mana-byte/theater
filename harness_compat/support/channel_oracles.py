"""Observe exact hook events and channel/tool identity through the public frontend client."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from harness_compat.support.live_rig import inconclusive, violation
from theater.constants.daemon import BUS_KIND_AGENT_HARNESS_EVENT
from theater.frontend import FrontendClient
from theater.frontend.trajectory import (
    TrajectoryDelta,
    TrajectoryKind,
    TrajectoryPage,
    TrajectoryRecord,
)
from theater.harness.contracts.manifest import HookChannelManifest
from theater.trajectory.identity import namespaced_native_id

_OBSERVATION_SECONDS = 20.0
_POLL_SECONDS = 0.5


@asynccontextmanager
async def capture_channel_facts(
    client: FrontendClient, participant: str
) -> AsyncIterator[TrajectoryPage]:
    """Open before the turn: enrichment facts have no durable history to replay."""
    snapshot = await client.trajectory.snapshot(participant)
    page = TrajectoryPage.from_wire(snapshot.response.to_wire()["result"])
    assert page.stream_id is not None and page.cursor is not None, page.panel_state
    try:
        yield page
    finally:
        await client.trajectory.close(page.stream_id)


async def expect_channel_facts(
    client: FrontendClient,
    participant: str,
    channel: HookChannelManifest,
    page: TrajectoryPage,
    call_id: str | None,
    *,
    event: str | None = None,
    turn_started_at: float,
    evidence: Path,
    seconds: float = _OBSERVATION_SECONDS,
) -> None:
    """Require an exact event's admitted projection, or the channel's tool correlation."""
    assert page.stream_id is not None and page.cursor is not None
    declared = {binding.event for binding in channel.bindings}
    assert event is None or event in declared
    assert event is not None or call_id is not None
    # Only new records after the pre-turn cursor can prove this tool turn.
    previous_ids = {record.record_id for record in page.records}
    records: dict[str, TrajectoryRecord] = {}
    deliveries: list[dict[str, object]] = []
    after_id = 0
    cursor = page.cursor
    started = time.monotonic()
    started_at = time.time()
    try:
        while True:
            tail = (
                await client.diagnostics.bus_tail(
                    after_id=after_id, kinds=[BUS_KIND_AGENT_HARNESS_EVENT], wait_seconds=0
                )
            ).value
            for row in tail.items:
                assert isinstance(row, Mapping)
                row_id = row["id"]
                assert isinstance(row_id, int)
                after_id = max(after_id, row_id)
                payload = row["payload"]
                if row["to_id"] != participant or not isinstance(payload, Mapping):
                    continue
                if payload.get("channel") != channel.declaration.id:
                    continue
                delivery: dict[str, object] = {"id": row_id, "ts": row["ts"], **payload}
                deliveries.append(delivery)
                delivered_event = payload.get("event")
                assert isinstance(delivered_event, str) and delivered_event in declared, (
                    f"undeclared event on {channel.declaration.id}: {delivery}; expected {declared}"
                )

            accepted = [
                row
                for row in deliveries
                if row.get("dropped") is False
                and row["ts"] >= turn_started_at
                and (event is None or row["event"] == event)
            ]
            if event is not None:
                projected = _event_projected(
                    records.values(), accepted, participant, event, call_id
                )
            else:
                assert call_id is not None
                projected = _correlated(records.values(), participant, call_id)
            if accepted and projected:
                return

            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                assert accepted, (
                    f"no accepted event for participant {participant} on channel "
                    f"{channel.declaration.id} event={event} within {seconds:g}s; "
                    f"deliveries={deliveries}"
                )
                violation(
                    f"channel {channel.declaration.id} did not project event={event} "
                    f"or correlate call={call_id} within {seconds:g}s"
                )
            followed = await client.trajectory.follow(
                page.stream_id, cursor, wait_seconds=min(_POLL_SECONDS, remaining)
            )
            delta = TrajectoryDelta.from_wire(followed.response.to_wire()["result"])
            if delta.resync_required:
                inconclusive(f"the trajectory stream needs resync: {delta.reason}")
            assert delta.cursor is not None
            cursor = delta.cursor
            records.update(
                (item.record.record_id, item.record)
                for item in delta.upserts
                if item.record.record_id not in previous_ids
            )
    finally:
        evidence.write_text(
            json.dumps(
                {
                    "participant": participant,
                    "channel": channel.declaration.id,
                    "call_id": call_id,
                    "event": event,
                    "turn_started_at": turn_started_at,
                    "poll_started_at": started_at,
                    "observation_seconds": time.monotonic() - started,
                    "deliveries": deliveries,
                    "previous_record_ids": sorted(previous_ids),
                    "records": [record.to_wire() for record in records.values()],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def _correlated(
    records: Collection[TrajectoryRecord],
    participant: str,
    call_id: str,
) -> bool:
    primary_sources = {
        record.source
        for record in records
        if record.participant_id == participant
        and record.kind is TrajectoryKind.TOOL_CALL
        and record.call_id == call_id
    }
    return bool(primary_sources) and any(
        record.participant_id == participant
        and record.source not in primary_sources
        and _carries_value(record, call_id)
        for record in records
    )


def _event_projected(
    records: Collection[TrajectoryRecord],
    accepted: Collection[Mapping[str, object]],
    participant: str,
    event: str,
    call_id: str | None,
) -> bool:
    native_ids = {row["native_id"] for row in accepted if isinstance(row.get("native_id"), str)}
    primary_sources = {
        record.source
        for record in records
        if record.kind in (TrajectoryKind.USER, TrajectoryKind.ASSISTANT, TrajectoryKind.TOOL_CALL)
    }
    # Tool lifecycle revisions may coalesce; their exact tool id still proves the projection.
    value = event if call_id is None else call_id
    return any(
        record.participant_id == participant
        and record.source not in primary_sources
        and any(
            record.native_id == namespaced_native_id(native_id, record.source_epoch)
            for native_id in native_ids
        )
        and _carries_value(record, value)
        for record in records
    )


def _carries_value(record: TrajectoryRecord, value: str) -> bool:
    """Match complete values, never a plugin-specific detail name or an id substring."""
    return value in (record.call_id, record.native_id) or any(
        detail.preview.text == value and not detail.preview.omitted_bytes
        for detail in record.details
    )
