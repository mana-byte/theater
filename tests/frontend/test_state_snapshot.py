"""Daemon state snapshots: immutable pages, route and action facts, cache bounds."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import insert

from tests.support.frontend_wire import request
from tests.support.state_stream import call_public, close_writer, journal_record, public_connection
from theater.daemon.events.publication import (
    next_revision,
    participant_event,
)
from theater.daemon.events.reader import StateReadError
from theater.daemon.events.snapshot import (
    CachedParticipantProjection,
    ParticipantProjectionFacts,
    SnapshotService,
)
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.presence.contracts import PresenceSnapshot, PresenceState
from theater.daemon.schema import orchestration_events
from theater.harness.contracts.runtime import (
    ConnectionHealth,
    RuntimeCapabilities,
    RuntimeCapability,
    RuntimeLifecyclePhase,
    RuntimeWiring,
)
from theater.models import (
    ProviderRecord,
    PublicOperationRecord,
    Status,
    TerminalBindingRecord,
    WorkspaceRecord,
    WorkspaceUsageRecord,
)


def test_journal_tail_reads_do_not_scan_retained_history(store):
    store.conn.execute(
        insert(orchestration_events),
        [
            {
                "sequence": index,
                "transaction_id": f"group-{index}",
                "event_index": 0,
                "ending_sequence": index,
                "kind": "participant.updated",
                "entity_id": "agent",
                "entity_revision": index,
                "payload": "{}",
                "recorded_at": 1.0,
            }
            for index in range(1, 6001)
        ],
    )
    database = store.conn.connection.driver_connection
    steps = 0

    def budget():
        nonlocal steps
        steps += 100
        return int(steps > 5000)

    database.set_progress_handler(budget, 100)
    try:
        assert store.journal.has_ending_sequence(5999)
        assert store.journal.groups_after(6000, limit=200) == ()
        groups = store.journal.groups_after(5999, limit=1)
        assert len(groups) == 1 and groups[0].ending_sequence == 6000
        groups = store.journal.groups_after(0, limit=1)
        assert len(groups) == 1 and groups[0].ending_sequence == 1
    finally:
        database.set_progress_handler(None, 0)


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
            unit, [journal_record(first.id)], transaction_id="state-snapshot-event"
        )
    post_setup_cursor = daemon.store.journal.current_sequence()

    reader, writer = await public_connection("state-snapshot-client")
    try:
        snapshot_response = await call_public(
            reader, writer, request(2, "frontend.state.snapshot", {"page_size": 1})
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
        second_page = await call_public(
            reader,
            writer,
            request(
                3,
                "frontend.state.page",
                {"snapshot_id": snapshot["snapshot_id"], "page": 1},
            ),
        )
        assert second_page["result"]["participants"][0]["participant_id"] == second.id
        assert second_page["result"]["participants"][0]["status"] != "dead"
        assert second_page["result"]["complete"] is True

        released = await call_public(
            reader,
            writer,
            request(4, "frontend.state.release", {"snapshot_id": snapshot["snapshot_id"]}),
        )
        assert released["result"] == {"released": True}
        expired = await call_public(
            reader,
            writer,
            request(
                5,
                "frontend.state.page",
                {"snapshot_id": snapshot["snapshot_id"], "page": 0},
            ),
        )
        assert expired["error"]["code"] == "snapshot_expired"
    finally:
        await close_writer(writer)


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
    monkeypatch.setattr(
        daemon.presence,
        "snapshot_for_binding",
        lambda _participant_id, _binding, **_kwargs: PresenceSnapshot(
            PresenceState.ABSENT, "fixture", 1, 1.0
        ),
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

    def projection(
        current, _binding, _native, transactional, connection, _projected_online_provider
    ):
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


async def test_canonical_state_projection_keeps_transcript_identity_and_timestamps(
    daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    participant = daemon.registry.register(
        harness="codex",
        pane=None,
        cwd="/workspace/project",
        session_id="session-a",
    )
    participant.session_correlation = "proven"
    participant.transcript_location = "/tmp/transcript.jsonl"
    participant.transcript_domain = "/workspace/project"
    daemon.store.upsert_participant(participant)
    monkeypatch.setattr(
        daemon.observer,
        "transcript_identity_lost",
        lambda participant_id: participant_id == participant.id,
    )

    snapshot = daemon.state_service.snapshot("identity-state-client", page_size=500)
    projected = next(
        item for item in snapshot["participants"] if item["participant_id"] == participant.id
    )

    assert projected["transcript_identity"] == {
        "state": "lost",
        "session_id": "session-a",
        "provenance": "proven",
        "location": "/tmp/transcript.jsonl",
        "domain": "/workspace/project",
        "detail": "the previously trusted transcript identity was lost",
    }
    assert projected["resume_state"] == "live"
    assert projected["created_at"] == participant.created_at
    assert projected["last_activity"] == participant.last_activity


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
