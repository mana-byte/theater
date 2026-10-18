"""Daemon journal events and follow equal fresh snapshots; recovery and cursor resume."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from tests.support.frontend_wire import projection_wire, request
from tests.support.state_stream import call_public, close_writer, journal_record, public_connection
from theater import paths
from theater.daemon.events.publication import (
    next_revision,
    terminal_binding_event,
)
from theater.daemon.events.reader import JournalReader, StateReadError, StreamCursor
from theater.daemon.persistence.repositories.journal import JournalAppend
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.daemon.store import Store
from theater.frontend import EventCursor, FrontendClient, FrontendResponseError, StateSynchronizer
from theater.frontend.dto import Participant as PublicParticipant
from theater.frontend.dto import Provider as PublicProvider
from theater.models import (
    JournalEventRecord,
    Status,
    TerminalBindingRecord,
)
from theater.observability import tracing


def _append(daemon, *events: JournalEventRecord, transaction_id: str) -> JournalAppend:
    with daemon.store.write_unit() as unit:
        return daemon.store.journal.append_group(unit, list(events), transaction_id=transaction_id)


async def test_public_participant_events_equal_fresh_named_snapshots(daemon) -> None:
    reader, writer = await public_connection("state-participant-equality")
    try:
        cursor = {
            "stream_id": daemon.store.journal.stream_id(),
            "sequence": daemon.store.journal.current_sequence(),
        }
        participant = daemon.registry.register(
            harness="vibe", pane=None, cwd="/tmp/state-participant-equality"
        )
        created = await call_public(
            reader,
            writer,
            request(2, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        created_event = created["result"]["transactions"][0]["events"][0]
        created_snapshot = await call_public(
            reader,
            writer,
            request(3, "frontend.state.snapshot", {"page_size": 500}),
        )
        created_value = next(
            item
            for item in created_snapshot["result"]["participants"]
            if item["participant_id"] == participant.id
        )
        assert created_snapshot["result"]["ending_cursor"] == created["result"]["cursor"]
        assert PublicParticipant.from_wire(created_event["payload"]).to_wire() == (
            PublicParticipant.from_wire(created_value).to_wire()
        )
        assert created_value["name"] == participant.name

        cursor = created["result"]["cursor"]
        daemon.registry.update_metadata(
            participant.id,
            name="StateProjectionName",
            description="Canonical public metadata",
        )
        changed = await call_public(
            reader,
            writer,
            request(4, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        changed_event = changed["result"]["transactions"][0]["events"][0]
        changed_snapshot = await call_public(
            reader,
            writer,
            request(5, "frontend.state.snapshot", {"page_size": 500}),
        )
        changed_value = next(
            item
            for item in changed_snapshot["result"]["participants"]
            if item["participant_id"] == participant.id
        )
        assert changed_snapshot["result"]["ending_cursor"] == changed["result"]["cursor"]
        assert PublicParticipant.from_wire(changed_event["payload"]).to_wire() == (
            PublicParticipant.from_wire(changed_value).to_wire()
        )
        assert (changed_value["name"], changed_value["description"]) == (
            "StateProjectionName",
            "Canonical public metadata",
        )
    finally:
        await close_writer(writer)


async def test_public_provider_acquire_event_equals_fresh_health_snapshot(daemon) -> None:
    credential = "state-provider-secret"
    registered = daemon.terminal_service.registry.register(
        client_id="state-provider-client",
        idempotency_key="state-provider-register",
        params={
            "selector": "state-provider-equality",
            "kind": "fixture",
            "credential_verifier": credential_verifier(credential),
            "capabilities": ["terminal-provider.v1"],
            "limits": {"terminals": 2},
        },
    )
    provider_id = str(registered["provider_id"])
    cursor = {
        "stream_id": daemon.store.journal.stream_id(),
        "sequence": daemon.store.journal.current_sequence(),
    }
    daemon.terminal_service.connections.acquire_callback(provider_id, credential)

    reader, writer = await public_connection("state-provider-equality")
    try:
        followed = await call_public(
            reader,
            writer,
            request(2, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        provider_event = next(
            event
            for transaction in followed["result"]["transactions"]
            for event in transaction["events"]
            if event["kind"] == "provider.updated"
        )
        snapshot = await call_public(
            reader,
            writer,
            request(3, "frontend.state.snapshot", {"page_size": 500}),
        )
        provider_value = next(
            item for item in snapshot["result"]["providers"] if item["provider_id"] == provider_id
        )
        assert snapshot["result"]["ending_cursor"] == followed["result"]["cursor"]
        assert PublicProvider.from_wire(provider_event["payload"]).to_wire() == (
            PublicProvider.from_wire(provider_value).to_wire()
        )
        assert provider_value["health"] == "reconciling"
    finally:
        await close_writer(writer)


async def test_sdk_follow_matches_fresh_snapshot_for_private_registry_and_jobs(daemon) -> None:
    participant = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/state-sdk-private")
    client = FrontendClient(paths.socket_path(), client_id="state-sdk-private")
    synchronizer = StateSynchronizer(client)
    try:
        await synchronizer.refresh()

        daemon.registry.update_metadata(participant.id, description="updated privately")
        updated = await synchronizer.follow_once(wait_seconds=0)
        fresh = await synchronizer.refresh()
        assert updated.cursor == fresh.cursor
        assert projection_wire(updated) == projection_wire(fresh)
        assert updated.participants[participant.id].description == "updated privately"

        daemon.jobs.create(
            handle="state-sdk-private-job",
            caller_id="cli",
            target_id=participant.id,
            kind="send",
        )
        running = await synchronizer.follow_once(wait_seconds=0)
        fresh = await synchronizer.refresh()
        assert running.cursor == fresh.cursor
        assert projection_wire(running) == projection_wire(fresh)
        assert running.jobs["state-sdk-private-job"].state == "running"

        daemon.jobs.finish("state-sdk-private-job", state="done", result="finished")
        daemon.registry.set_status(participant.id, Status.DEAD)
        terminal = await synchronizer.follow_once(wait_seconds=0)
        fresh = await synchronizer.refresh()
        assert terminal.cursor == fresh.cursor
        assert projection_wire(terminal) == projection_wire(fresh)
        assert participant.id not in terminal.participants
        assert "state-sdk-private-job" not in terminal.jobs
    finally:
        await client.close()


async def test_new_binding_event_uses_the_same_transactional_route_as_fresh_snapshot(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    monkeypatch.setattr(
        daemon.terminal_service.connections,
        "is_current",
        lambda provider_id, generation: (provider_id, generation) == ("provider-new", 3),
    )
    monkeypatch.setattr(
        daemon.terminal_service.connections,
        "health",
        lambda provider_id: "online" if provider_id == "provider-new" else "offline",
    )
    monkeypatch.setattr(
        daemon.presence,
        "snapshot",
        lambda _participant_id: PresenceSnapshot(PresenceState.ABSENT, "fixture", 1, 1.0),
    )
    binding = TerminalBindingRecord(
        participant_id=participant.id,
        provider_id="provider-new",
        provider_generation=3,
        terminal_id="terminal-new",
        terminal_incarnation="incarnation-new",
        occupant_evidence={"occupant_id": "occupant-new"},
        health="healthy",
        report_revision=1,
        created_at=1.0,
        updated_at=1.0,
    )

    with daemon.store.write_unit() as unit:
        daemon.store.terminal_bindings.bind(binding, connection=unit.connection)
        persisted = daemon.store.terminal_bindings.get(participant.id, connection=unit.connection)
        assert persisted is not None
        event = terminal_binding_event(
            daemon.store,
            persisted,
            unit.connection,
            revision=next_revision(daemon.store, unit.connection),
            recorded_at=2.0,
        )
        daemon.store.journal.append_group(unit, [event])

    fresh = daemon.state_service.snapshot("binding-transaction-client", page_size=500)
    fresh_value = next(
        item for item in fresh["participants"] if item["participant_id"] == participant.id
    )
    assert event.payload["terminal_route"] == fresh_value["terminal_route"]
    assert event.payload["actions"] == fresh_value["actions"]
    assert event.payload["addressable"] is True
    assert event.payload["actions"]["send"]["route_available"] is True


async def test_follow_keeps_groups_whole_and_closes_waiter_races(daemon) -> None:
    stream_id = daemon.store.journal.stream_id()
    cursor = {"stream_id": stream_id, "sequence": 0}
    reader, writer = await public_connection("state-follow-client")
    try:
        empty = await call_public(
            reader,
            writer,
            request(
                2,
                "frontend.state.follow",
                {"cursor": cursor, "wait_seconds": 0, "limit": 1},
            ),
        )
        assert empty["result"] == {"transactions": [], "cursor": cursor, "timed_out": False}

        first = _append(
            daemon,
            journal_record("state-a"),
            journal_record("state-b", 2),
            transaction_id="state-group-a",
        )
        followed = await call_public(
            reader,
            writer,
            request(3, "frontend.state.follow", {"cursor": cursor, "limit": 1}),
        )
        transaction = followed["result"]["transactions"]
        assert len(transaction) == 1
        assert [event["entity_id"] for event in transaction[0]["events"]] == ["state-a", "state-b"]
        assert followed["result"]["cursor"]["sequence"] == first.ending_sequence

        replay = await call_public(
            reader,
            writer,
            request(4, "frontend.state.follow", {"cursor": cursor, "limit": 1}),
        )
        assert replay["result"] == followed["result"]

        boundary_error = await call_public(
            reader,
            writer,
            request(
                5,
                "frontend.state.follow",
                {"cursor": {"stream_id": stream_id, "sequence": 1}, "wait_seconds": 0},
            ),
        )
        assert boundary_error["error"]["code"] == "resnapshot_required"

        timeout = await call_public(
            reader,
            writer,
            request(
                6,
                "frontend.state.follow",
                {"cursor": followed["result"]["cursor"], "wait_seconds": 0.01},
            ),
        )
        assert timeout["result"]["transactions"] == []
        assert timeout["result"]["timed_out"] is True

        waiting = asyncio.create_task(
            daemon.state_service.follow(followed["result"]["cursor"], wait_seconds=1, limit=1)
        )
        for _ in range(10):
            if daemon.state_service.follows.waiter_count:
                break
            await asyncio.sleep(0)
        assert daemon.state_service.follows.waiter_count == 1
        second = _append(daemon, journal_record("state-c", 3), transaction_id="state-group-b")
        woken = await waiting
        assert woken["cursor"]["sequence"] == second.ending_sequence
        assert woken["timed_out"] is False

        cancelled = asyncio.create_task(
            daemon.state_service.follow(woken["cursor"], wait_seconds=30, limit=1)
        )
        for _ in range(10):
            if daemon.state_service.follows.waiter_count:
                break
            await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert daemon.state_service.follows.waiter_count == 0

        with daemon.store.write_unit() as unit:
            daemon.store.journal.delete_through(first.ending_sequence, connection=unit.connection)
        retention = await call_public(
            reader,
            writer,
            request(7, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        assert retention["error"]["code"] == "resnapshot_required"
        mismatch = await call_public(
            reader,
            writer,
            request(
                8,
                "frontend.state.follow",
                {"cursor": {"stream_id": "replacement-stream", "sequence": 0}},
            ),
        )
        assert mismatch["error"]["code"] == "resnapshot_required"
    finally:
        await close_writer(writer)


def test_journal_cursor_resumes_after_restart_but_not_database_replacement(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = Store(path)
    try:
        with store.write_unit() as unit:
            appended = store.journal.append_group(
                unit, [journal_record("restart-a")], transaction_id="restart-group"
            )
        cursor = StreamCursor(appended.stream_id, appended.ending_sequence)
    finally:
        store.close()

    reopened = Store(path)
    replacement = Store(tmp_path / "replacement.db")
    try:
        resumed = JournalReader(reopened.journal).read(cursor, limit=1)
        assert resumed.transactions == ()
        assert resumed.cursor == cursor
        with pytest.raises(StateReadError, match="different database stream"):
            JournalReader(replacement.journal).read(cursor, limit=1)
    finally:
        reopened.close()
        replacement.close()


@pytest.mark.parametrize(
    ("case", "code", "failed"),
    [
        ("follow", "resnapshot_required", False),
        ("expired_page", "snapshot_expired", False),
        ("bad_page", "bad_request", True),
    ],
)
async def test_recovery_response_keeps_wire_code_and_honest_span_status(
    daemon, monkeypatch, case, code, failed
):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "_get_tracer", lambda: provider.get_tracer("test"))
    client = FrontendClient(paths.socket_path(), client_id="recovery-timing")
    try:
        snapshot_id = "expired"
        if case == "bad_page":
            snapshot_id = (await client.state.snapshot()).value.snapshot_id
        with pytest.raises(FrontendResponseError) as caught:
            if case == "follow":
                await client.state.follow(EventCursor("retired-stream", 0), wait_seconds=0)
            else:
                await client.state.page(snapshot_id, 999)
        assert caught.value.value.code == code
        method = "follow" if case == "follow" else "page"
        spans = [
            span
            for span in exporter.get_finished_spans()
            if span.name == f"rpc.server frontend.state.{method}"
        ]
        assert len(spans) == 1
        assert (spans[0].status.status_code is StatusCode.ERROR) is failed
        assert spans[0].attributes.get("theater.rpc.recovery") == (None if failed else code)
    finally:
        await client.close()
        provider.shutdown()
