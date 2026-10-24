"""Observe channel delivery and tool identity through the public frontend client."""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Collection, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from tests.support.live_rig import inconclusive, violation
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
    """Open before the tool turn: enrichment facts have no durable history to replay."""
    snapshot = await client.trajectory.snapshot(participant)
    page = TrajectoryPage.from_wire(snapshot.response.to_wire()["result"])
    assert page.stream_id is not None and page.cursor is not None, page.panel_state
    try:
        yield page
    finally:
        await client.trajectory.close(page.stream_id)


async def expect_channel_tool_facts(
    client: FrontendClient,
    participant: str,
    channel: HookChannelManifest,
    page: TrajectoryPage,
    call_id: str,
    *,
    evidence: Path,
    seconds: float = _OBSERVATION_SECONDS,
) -> None:
    """Require accepted hook delivery and a second source carrying the scripted call id."""
    assert page.stream_id is not None and page.cursor is not None
    declared = {binding.event for binding in channel.bindings}
    records = {record.record_id: record for record in page.records}
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
                event = payload.get("event")
                assert isinstance(event, str) and event in declared, (
                    f"undeclared event on {channel.declaration.id}: {delivery}; expected {declared}"
                )

            accepted = [row for row in deliveries if row.get("dropped") is False]
            if accepted:
                identities = [row.get("native_id") for row in accepted]
                if not all(isinstance(value, str) and value for value in identities):
                    inconclusive("hook bus rows expose no native_id to attribute trajectory facts")
                native_ids = {value for value in identities if isinstance(value, str)}
                if _correlated(records.values(), participant, native_ids, call_id):
                    return

            remaining = seconds - (time.monotonic() - started)
            if remaining <= 0:
                assert accepted, (
                    f"no accepted event for participant {participant} on channel "
                    f"{channel.declaration.id} within {seconds:g}s; deliveries={deliveries}"
                )
                violation(
                    f"channel {channel.declaration.id} and the primary tool facts did not "
                    f"correlate on {call_id} from distinct sources within {seconds:g}s"
                )
            followed = await client.trajectory.follow(
                page.stream_id, cursor, wait_seconds=min(_POLL_SECONDS, remaining)
            )
            delta = TrajectoryDelta.from_wire(followed.response.to_wire()["result"])
            if delta.resync_required:
                inconclusive(f"the trajectory stream needs resync: {delta.reason}")
            assert delta.cursor is not None
            cursor = delta.cursor
            records.update((item.record.record_id, item.record) for item in delta.upserts)
    finally:
        evidence.write_text(
            json.dumps(
                {
                    "participant": participant,
                    "channel": channel.declaration.id,
                    "call_id": call_id,
                    "poll_started_at": started_at,
                    "observation_seconds": time.monotonic() - started,
                    "deliveries": deliveries,
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
    native_ids: set[str],
    call_id: str,
) -> bool:
    primary_sources = {
        record.source
        for record in records
        if record.participant_id == participant
        and record.kind is TrajectoryKind.TOOL_CALL
        and record.call_id == call_id
    }
    return any(
        record.participant_id == participant
        and any(
            record.native_id == namespaced_native_id(native_id, record.source_epoch)
            for native_id in native_ids
        )
        and _carries_call_id(record, call_id)
        and any(source != record.source for source in primary_sources)
        for record in records
    )


def _carries_call_id(record: TrajectoryRecord, call_id: str) -> bool:
    """Match complete values, never a plugin-specific detail name or an id substring."""
    return (
        record.call_id == call_id
        or record.native_id == namespaced_native_id(call_id, record.source_epoch)
        or any(
            detail.preview.text == call_id and not detail.preview.omitted_bytes
            for detail in record.details
        )
    )
