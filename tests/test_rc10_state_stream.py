"""Focused public snapshot and orchestration-journal follow coverage."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from theater import paths, protocol
from theater.daemon.events.publication import (
    next_revision,
    participant_event,
    terminal_binding_event,
)
from theater.daemon.events.reader import JournalReader, StateReadError, StreamCursor
from theater.daemon.events.snapshot import (
    CachedParticipantProjection,
    ParticipantProjectionFacts,
    SnapshotService,
)
from theater.daemon.persistence.repositories.journal import JournalAppend
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.daemon.store import Store
from theater.frontend import FrontendClient, StateProjection, StateSynchronizer
from theater.frontend.capabilities import PUBLIC_API_MAJOR, PUBLIC_API_MINOR
from theater.frontend.dto import Participant as PublicParticipant
from theater.frontend.dto import Provider as PublicProvider
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import (
    JournalEventRecord,
    ProviderRecord,
    PublicOperationRecord,
    Status,
    TerminalBindingRecord,
    WorkspaceRecord,
    WorkspaceUsageRecord,
)


def _request(request_id: int, method: str, params: dict[str, object] | None = None) -> bytes:
    return protocol.encode({"id": request_id, "method": method, "params": params or {}})


def _handshake(client_id: str) -> bytes:
    return _request(
        1,
        "frontend.handshake",
        {
            "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
            "client_id": client_id,
            "role": "operator",
            "channel": "rpc",
            "required_capabilities": [],
        },
    )


async def _public_connection(client_id: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(
        str(paths.socket_path()), limit=max(protocol.MAX_MESSAGE_BYTES, 1024)
    )
    writer.write(_handshake(client_id))
    await writer.drain()
    response = json.loads(await protocol.read_message(reader))
    assert response["ok"] is True
    return reader, writer


async def _call(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, frame: bytes) -> dict:
    writer.write(frame)
    await writer.drain()
    return json.loads(await protocol.read_message(reader))


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()


def _projection_wire(projection: StateProjection) -> dict[str, dict[str, object]]:
    return {
        "participants": {key: value.to_wire() for key, value in projection.participants.items()},
        "operations": {key: value.to_wire() for key, value in projection.operations.items()},
        "jobs": {key: value.to_wire() for key, value in projection.jobs.items()},
        "providers": {key: value.to_wire() for key, value in projection.providers.items()},
        "workspaces": {key: value.to_wire() for key, value in projection.workspaces.items()},
    }


def _event(entity_id: str, revision: int = 1) -> JournalEventRecord:
    return JournalEventRecord(
        kind="participant.updated",
        entity_id=entity_id,
        entity_revision=revision,
        payload={"participant_id": entity_id},
        recorded_at=1.0,
    )


def _append(daemon, *events: JournalEventRecord, transaction_id: str) -> JournalAppend:
    with daemon.store.write_unit() as unit:
        return daemon.store.journal.append_group(unit, list(events), transaction_id=transaction_id)


async def test_snapshot_pages_are_immutable_active_and_publicly_validated(daemon) -> None:
    first = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/state-1")
    second = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/state-2")
    dead = daemon.registry.register(harness="vibe", pane=None, cwd="/tmp/state-dead")
    daemon.registry.set_status(dead.id, Status.DEAD)
    daemon.jobs.create(
        handle="state-job",
        caller_id="state-caller",
        target_id=first.id,
        kind="send",
    )
    with daemon.store.write_unit() as unit:
        daemon.store.operations.create(
            PublicOperationRecord(
                operation_id="state-operation",
                kind="send",
                actor_client_id="state-client",
                actor_participant_id=None,
                target_ids=(first.id,),
                state="accepted",
                phase="accepted",
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )
        daemon.store.providers.register(
            ProviderRecord(
                provider_id="state-provider",
                selector="state-provider",
                kind="fixture",
                credential_verifier="not-a-credential",
                configuration_version=1,
                capabilities=("terminal-provider.v1",),
                limits={"terminals": 1},
                generation=2,
                last_report_revision=3,
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )
        workspace = WorkspaceRecord(
            workspace_id="state-workspace",
            ownership_kind="theater",
            owner_id="state-client",
            path="/tmp/state-workspace",
            state="active",
            created_at=1.0,
            updated_at=1.0,
        )
        daemon.store.workspaces.create(workspace, connection=unit.connection)
        assert daemon.store.workspaces.acquire_usage(
            WorkspaceUsageRecord(
                usage_id="state-usage",
                workspace_id=workspace.workspace_id,
                holder_kind="participant",
                holder_id=first.id,
                acquired_at=1.0,
            ),
            connection=unit.connection,
        )
        daemon.store.journal.append_group(
            unit, [_event(first.id)], transaction_id="state-snapshot-event"
        )
    post_setup_cursor = daemon.store.journal.current_sequence()

    reader, writer = await _public_connection("state-snapshot-client")
    try:
        snapshot_response = await _call(
            reader, writer, _request(2, "frontend.state.snapshot", {"page_size": 1})
        )
        snapshot = snapshot_response["result"]
        assert snapshot_response["ok"] is True
        assert snapshot["ending_cursor"]["sequence"] == post_setup_cursor
        assert snapshot["participants"][0]["participant_id"] == first.id
        assert snapshot["operations"][0]["operation_id"] == "state-operation"
        assert snapshot["jobs"][0]["handle"] == "state-job"
        assert snapshot["providers"][0]["health"] == "offline"
        assert snapshot["workspaces"][0]["usages"][0]["usage_id"] == "state-usage"
        assert dead.id not in {item["participant_id"] for item in snapshot["participants"]}

        daemon.registry.set_status(second.id, Status.DEAD)
        second_page = await _call(
            reader,
            writer,
            _request(
                3,
                "frontend.state.page",
                {"snapshot_id": snapshot["snapshot_id"], "page": 1},
            ),
        )
        assert second_page["result"]["participants"][0]["participant_id"] == second.id
        assert second_page["result"]["participants"][0]["status"] != "dead"
        assert second_page["result"]["complete"] is True

        released = await _call(
            reader,
            writer,
            _request(4, "frontend.state.release", {"snapshot_id": snapshot["snapshot_id"]}),
        )
        assert released["result"] == {"released": True}
        expired = await _call(
            reader,
            writer,
            _request(
                5,
                "frontend.state.page",
                {"snapshot_id": snapshot["snapshot_id"], "page": 0},
            ),
        )
        assert expired["error"]["code"] == "snapshot_expired"
    finally:
        await _close(writer)


async def test_public_participant_events_equal_fresh_named_snapshots(daemon) -> None:
    reader, writer = await _public_connection("state-participant-equality")
    try:
        cursor = {
            "stream_id": daemon.store.journal.stream_id(),
            "sequence": daemon.store.journal.current_sequence(),
        }
        participant = daemon.registry.register(
            harness="vibe", pane=None, cwd="/tmp/state-participant-equality"
        )
        created = await _call(
            reader,
            writer,
            _request(2, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        created_event = created["result"]["transactions"][0]["events"][0]
        created_snapshot = await _call(
            reader,
            writer,
            _request(3, "frontend.state.snapshot", {"page_size": 500}),
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
        changed = await _call(
            reader,
            writer,
            _request(4, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        changed_event = changed["result"]["transactions"][0]["events"][0]
        changed_snapshot = await _call(
            reader,
            writer,
            _request(5, "frontend.state.snapshot", {"page_size": 500}),
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
        await _close(writer)


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

    reader, writer = await _public_connection("state-provider-equality")
    try:
        followed = await _call(
            reader,
            writer,
            _request(2, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        provider_event = next(
            event
            for transaction in followed["result"]["transactions"]
            for event in transaction["events"]
            if event["kind"] == "provider.updated"
        )
        snapshot = await _call(
            reader,
            writer,
            _request(3, "frontend.state.snapshot", {"page_size": 500}),
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
        await _close(writer)


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
        assert _projection_wire(updated) == _projection_wire(fresh)
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
        assert _projection_wire(running) == _projection_wire(fresh)
        assert running.jobs["state-sdk-private-job"].state == "running"

        daemon.jobs.finish("state-sdk-private-job", state="done", result="finished")
        daemon.registry.set_status(participant.id, Status.DEAD)
        terminal = await synchronizer.follow_once(wait_seconds=0)
        fresh = await synchronizer.refresh()
        assert terminal.cursor == fresh.cursor
        assert _projection_wire(terminal) == _projection_wire(fresh)
        assert participant.id not in terminal.participants
        assert "state-sdk-private-job" not in terminal.jobs
    finally:
        await client.close()


async def test_snapshot_does_not_infer_provider_addressability_from_durable_health(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    with daemon.store.write_unit() as unit:
        daemon.store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant.id,
                provider_id="offline-provider",
                provider_generation=4,
                terminal_id="terminal-offline",
                terminal_incarnation="incarnation-offline",
                occupant_evidence={"occupant_id": "occupant-offline"},
                health="healthy",
                report_revision=7,
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )

    snapshot = SnapshotService(daemon.store).snapshot("offline-provider-client", page_size=1)
    projected = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )
    assert projected["terminal_route"]["health"] == "offline"
    assert projected["addressable"] is False
    assert projected["actions"]["send"]["route_available"] is False


async def test_snapshot_and_participant_event_use_cached_public_route_facts(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    credential = "state-projection-provider"
    provider = daemon.terminal_service.registry.register(
        client_id="state-projection-client",
        idempotency_key="state-projection-register",
        params={
            "selector": "state-projection-provider",
            "kind": "fixture",
            "credential_verifier": credential_verifier(credential),
            "capabilities": ["terminal-provider.v1"],
            "limits": {"terminals": 1},
        },
    )
    provider_id = str(provider["provider_id"])
    generation, _token = daemon.terminal_service.connections.acquire_callback(
        provider_id, credential
    )
    with daemon.store.write_unit() as unit:
        daemon.store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant.id,
                provider_id=provider_id,
                provider_generation=generation,
                terminal_id="state-projection-terminal",
                terminal_incarnation="state-projection-incarnation",
                occupant_evidence={"occupant_id": "state-projection-occupant"},
                health="healthy",
                report_revision=1,
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )
    daemon.terminal_service.connections.mark_online(provider_id, generation)
    monkeypatch.setattr(
        daemon.presence,
        "snapshot",
        lambda _participant_id: PresenceSnapshot(PresenceState.ABSENT, "fixture", 1, 1.0),
    )

    with daemon.store.write_unit() as unit:
        event = participant_event(
            daemon.store,
            daemon.registry.get(participant.id),
            unit.connection,
            revision=next_revision(daemon.store, unit.connection),
            recorded_at=2.0,
            kind="participant.controls_changed",
        )
        daemon.store.journal.append_group(unit, [event])

    snapshot = daemon.state_service.snapshot("state-projection-client", page_size=500)
    projected = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )
    assert projected["presence"] == "absent"
    assert projected["addressable"] is True
    assert projected["terminal_route"]["identity"]["provider_generation"] == generation
    assert projected["actions"]["send"] == {
        "supported": True,
        "route_available": True,
        "admissible": True,
        "reason": None,
        "detail": None,
    }
    assert event.payload["presence"] == "absent"
    assert event.payload["actions"] == projected["actions"]
    assert event.payload["addressable"] is True


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


async def test_snapshot_action_facts_share_the_materialization_transaction(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    created = False

    def participant_name(_participant_id: str) -> None:
        nonlocal created
        if not created:
            created = True
            daemon.jobs.create(
                handle="committed-during-snapshot",
                caller_id="cli",
                target_id=participant.id,
                kind="send",
            )

    def projection(current, _binding, _native, transactional, connection):
        running = daemon.store.active_running_jobs_for_target(
            current.id,
            connection=connection if transactional else None,
        )
        action = {
            "supported": True,
            "route_available": True,
            "admissible": not running,
            "reason": "busy" if running else None,
            "detail": None,
        }
        return ParticipantProjectionFacts(
            presence="absent",
            terminal_route=None,
            native_route=None,
            actions={capability.value: action for capability in RuntimeCapability},
            addressable=True,
        )

    snapshot = SnapshotService(
        daemon.store,
        participant_name=participant_name,
        participant_projection=projection,
    ).snapshot("transaction-race-client", page_size=500)
    projected = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    assert created is True
    assert projected["actions"]["send"]["admissible"] is True
    assert snapshot["jobs"] == []
    assert daemon.store.active_running_jobs_for_target(participant.id)


async def test_snapshot_keeps_durable_native_and_trusted_identity_without_live_runtime(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = daemon.registry.register(
        harness="codex", pane=None, cwd=None, session_id="trusted-session"
    )
    participant.session_correlation = "proven"
    daemon.store.upsert_participant(participant)
    with daemon.store.write_unit() as unit:
        daemon.store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id=participant.id,
                harness="codex",
                wiring=RuntimeWiring.NATIVE,
                backend_generation=9,
                lifecycle=RuntimeLifecyclePhase.BOUND,
                native_session_id="native-session",
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )

    def live_runtime_was_consulted(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("snapshot must not consult live runtime state")

    monkeypatch.setattr(daemon.runtime_manager, "get", live_runtime_was_consulted)
    snapshot = SnapshotService(daemon.store).snapshot("restart-state-client", page_size=1)
    projected = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )
    assert projected["native_route"] == {
        "backend_generation": 9,
        "native_session_id": "native-session",
        "health": "disconnected",
    }
    assert projected["trusted_identity"] == {
        "session_id": "trusted-session",
        "provenance": "proven",
    }
    assert projected["addressable"] is False


async def test_snapshot_uses_injected_cached_native_route_without_runtime_io(daemon) -> None:
    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    with daemon.store.write_unit() as unit:
        daemon.store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id=participant.id,
                harness="codex",
                wiring=RuntimeWiring.NATIVE,
                backend_generation=11,
                lifecycle=RuntimeLifecyclePhase.ACTIVE,
                native_session_id="cached-native-session",
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )

    route = SimpleNamespace(
        transport="native_runtime",
        is_provider=False,
        is_native=True,
        unavailable_reason=None,
        native_capabilities=RuntimeCapabilities(available=frozenset(RuntimeCapability)),
    )
    projection = CachedParticipantProjection(
        presence_snapshot=lambda _participant_id: PresenceSnapshot(
            PresenceState.ABSENT, "fixture", 1, 1.0
        ),
        terminal_projection=lambda _binding: {},
        route_for=lambda _participant_id, _capability: route,
        provider_health=lambda _provider_id, _generation: "offline",
        native_route=lambda _participant, durable: {
            **(durable or {}),
            "health": ConnectionHealth.CONNECTED.value,
        },
    )

    snapshot = SnapshotService(
        daemon.store,
        participant_projection=projection,
    ).snapshot("cached-native-client", page_size=1)
    projected = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )
    assert projected["native_route"] == {
        "backend_generation": 11,
        "native_session_id": "cached-native-session",
        "health": "connected",
    }
    assert projected["addressable"] is True
    assert projected["actions"]["send"]["admissible"] is True


async def test_snapshot_cache_expires_and_refuses_overflow(daemon) -> None:
    timestamp = 100.0

    def clock() -> float:
        return timestamp

    names = iter(("snapshot-a", "snapshot-b", "snapshot-c"))
    service = SnapshotService(daemon.store, clock=clock, id_factory=lambda: next(names))
    first = service.snapshot("client-a", page_size=1)
    first_snapshot_id = first["snapshot_id"]
    assert isinstance(first_snapshot_id, str)
    service.snapshot("client-a", page_size=1)
    with pytest.raises(StateReadError, match="maximum number"):
        service.snapshot("client-a", page_size=1)
    with pytest.raises(StateReadError, match="belongs to another client"):
        service.page("client-b", first_snapshot_id, 0)

    timestamp += 61.0
    with pytest.raises(StateReadError, match="missing, expired"):
        service.page("client-a", first_snapshot_id, 0)
    assert service.cache.used_bytes == 0

    too_small = SnapshotService(
        daemon.store,
        clock=clock,
        id_factory=lambda: "snapshot-small",
        cache_bytes=1,
    )
    with pytest.raises(StateReadError, match="cannot fit"):
        too_small.snapshot("client-b", page_size=1)


async def test_follow_keeps_groups_whole_and_closes_waiter_races(daemon) -> None:
    stream_id = daemon.store.journal.stream_id()
    cursor = {"stream_id": stream_id, "sequence": 0}
    reader, writer = await _public_connection("state-follow-client")
    try:
        empty = await _call(
            reader,
            writer,
            _request(
                2,
                "frontend.state.follow",
                {"cursor": cursor, "wait_seconds": 0, "limit": 1},
            ),
        )
        assert empty["result"] == {"transactions": [], "cursor": cursor, "timed_out": False}

        first = _append(
            daemon,
            _event("state-a"),
            _event("state-b", 2),
            transaction_id="state-group-a",
        )
        followed = await _call(
            reader,
            writer,
            _request(3, "frontend.state.follow", {"cursor": cursor, "limit": 1}),
        )
        transaction = followed["result"]["transactions"]
        assert len(transaction) == 1
        assert [event["entity_id"] for event in transaction[0]["events"]] == ["state-a", "state-b"]
        assert followed["result"]["cursor"]["sequence"] == first.ending_sequence

        replay = await _call(
            reader,
            writer,
            _request(4, "frontend.state.follow", {"cursor": cursor, "limit": 1}),
        )
        assert replay["result"] == followed["result"]

        boundary_error = await _call(
            reader,
            writer,
            _request(
                5,
                "frontend.state.follow",
                {"cursor": {"stream_id": stream_id, "sequence": 1}, "wait_seconds": 0},
            ),
        )
        assert boundary_error["error"]["code"] == "resnapshot_required"

        timeout = await _call(
            reader,
            writer,
            _request(
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
        second = _append(daemon, _event("state-c", 3), transaction_id="state-group-b")
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
        retention = await _call(
            reader,
            writer,
            _request(7, "frontend.state.follow", {"cursor": cursor, "wait_seconds": 0}),
        )
        assert retention["error"]["code"] == "resnapshot_required"
        mismatch = await _call(
            reader,
            writer,
            _request(
                8,
                "frontend.state.follow",
                {"cursor": {"stream_id": "replacement-stream", "sequence": 0}},
            ),
        )
        assert mismatch["error"]["code"] == "resnapshot_required"
    finally:
        await _close(writer)


def test_journal_cursor_resumes_after_restart_but_not_database_replacement(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    store = Store(path)
    try:
        with store.write_unit() as unit:
            appended = store.journal.append_group(
                unit, [_event("restart-a")], transaction_id="restart-group"
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
