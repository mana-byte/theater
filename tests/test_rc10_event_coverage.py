"""MVP coverage for transactional RC10 event publication."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from sqlalchemy import select, update

from theater.config import RetentionSection
from theater.constants import SECONDS_PER_DAY
from theater.daemon.control_ownership import ControlTransferService
from theater.daemon.events.reader import JournalReader, StreamCursor
from theater.daemon.events.snapshot import SnapshotService
from theater.daemon.gc import _sweep_journal, sweep
from theater.daemon.jobs import JobManager
from theater.daemon.operations import OperationService
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.registry import Registry
from theater.daemon.runtime.public_recovery import fail_proven_undispatched
from theater.daemon.schema import orchestration_events
from theater.daemon.worktrees.service import WorkspaceRequest, WorkspaceService
from theater.frontend.dto import Job as PublicJob
from theater.frontend.dto import Operation as PublicOperation
from theater.frontend.dto import Participant as PublicParticipant
from theater.frontend.dto import Workspace as PublicWorkspace
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
)
from theater.models import (
    Job,
    JobState,
    JournalEventRecord,
    LaunchReservationRecord,
    Participant,
    PublicOperationRecord,
    Status,
    TerminalBindingRecord,
    Tier,
    now,
)


def _events_after(store, sequence: int):
    return tuple(
        event for group in store.journal.groups_after(sequence, limit=500) for event in group.events
    )


def test_reparent_persists_and_publishes_only_a_changed_lineage(store) -> None:
    registry = Registry(store)
    participant = registry.register(harness="vibe", pane=None, cwd="/tmp/event-lineage")
    cursor = store.journal.current_sequence()

    store.reparent_participant(participant.id, new_parent_id="external-parent")

    persisted = store.get_participant(participant.id)
    assert persisted is not None and persisted.parent_id == "external-parent"
    events = _events_after(store, cursor)
    assert len(events) == 1
    assert events[0].kind == "participant.updated"
    assert PublicParticipant.from_wire(events[0].payload).parent_id == "external-parent"

    store.reparent_participant(participant.id, new_parent_id="external-parent")
    store.reparent_participant("missing-participant", new_parent_id="external-parent")
    assert store.journal.current_sequence() == events[0].sequence


async def test_private_registry_hook_and_natural_completion_publish(store) -> None:
    registry = Registry(store)
    jobs = JobManager(store)
    participant = registry.register(harness="vibe", pane=None, cwd="/tmp/event-private")
    created = _events_after(store, 0)
    assert created[-1].kind == "participant.updated"
    assert created[-1].payload["participant_id"] == participant.id

    cursor = store.journal.current_sequence()
    receipt = store.record_transcript_receipt(
        participant.id,
        session_id="session-event",
        transcript_location="/tmp/event-private/transcript.jsonl",
    )
    assert receipt is not None
    receipt_event = _events_after(store, cursor)
    assert len(receipt_event) == 1
    assert receipt_event[0].payload["trusted_identity"] == {
        "session_id": "session-event",
        "provenance": "exact",
    }

    cursor = store.journal.current_sequence()
    jobs.create(
        handle="event-job",
        caller_id="cli",
        target_id=participant.id,
        kind="send",
    )
    jobs.finish("event-job", state=JobState.DONE, result="finished from evidence")
    job_events = _events_after(store, cursor)
    assert [event.kind for event in job_events] == ["job.updated", "job.updated"]
    assert job_events[0].payload["state"] == "running"
    assert job_events[1].payload["state"] == "done"
    assert job_events[1].payload["raw_result"] == "finished from evidence"


def test_registry_and_job_rollback_expose_neither_cache_nor_events(
    store, monkeypatch, tmp_path: Path
) -> None:
    registry = Registry(store)
    jobs = JobManager(store)
    participant = registry.register(harness="vibe", pane=None, cwd="/tmp/event-rollback")
    jobs.create(
        handle="event-rollback-job",
        caller_id="cli",
        target_id=participant.id,
        kind="send",
        cwd=str(tmp_path),
    )
    original_name = participant.name
    cursor = store.journal.current_sequence()
    notifications: list[int] = []
    store.journal.register_listener(notifications.append)

    def fail_append(*_args, **_kwargs):
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(store.journal, "append_group", fail_append)
    names_before = dict(registry._names)
    participant_ids_before = {item.id for item in store.list_participants()}
    with pytest.raises(RuntimeError, match="injected event failure"):
        registry.update_metadata(
            participant.id,
            name="ScapinEvent",
            description="must roll back",
        )
    with pytest.raises(RuntimeError, match="injected event failure"):
        jobs.finish("event-rollback-job", state=JobState.DONE, result="must roll back")
    with pytest.raises(RuntimeError, match="injected event failure"):
        store.reparent_participant(participant.id, new_parent_id="must-roll-back")
    with pytest.raises(RuntimeError, match="injected event failure"):
        registry.create_spawned(harness="vibe", cwd="/tmp/event-creation-rollback")

    persisted = store.get_participant(participant.id)
    assert persisted is not None and persisted.description is None and persisted.parent_id is None
    assert store.get_job("event-rollback-job").state == JobState.RUNNING
    assert registry._names[participant.id] == original_name
    assert "event-rollback-job" in jobs._events
    assert "event-rollback-job" in jobs._accumulators
    assert registry._names == names_before
    assert {item.id for item in store.list_participants()} == participant_ids_before
    assert store.journal.current_sequence() == cursor
    assert notifications == []


async def test_provider_binding_and_catalog_events_track_visible_health(daemon) -> None:
    from theater.daemon.plugins.credentials import credential_verifier

    participant = daemon.registry.register(harness="codex", pane=None, cwd=None)
    credential = "provider-event-secret"
    provider = daemon.terminal_service.registry.register(
        client_id="event-operator",
        idempotency_key="event-provider-register",
        params={
            "selector": "event-provider",
            "kind": "fixture",
            "credential_verifier": credential_verifier(credential),
            "capabilities": ["terminal-provider.v1"],
            "limits": {"terminals": 4},
        },
    )
    provider_id = str(provider["provider_id"])
    with daemon.store.write_unit() as unit:
        daemon.store.terminal_bindings.bind(
            TerminalBindingRecord(
                participant_id=participant.id,
                provider_id=provider_id,
                provider_generation=0,
                terminal_id="event-terminal",
                terminal_incarnation="event-incarnation",
                occupant_evidence={"occupant_id": "event-occupant"},
                process_facts={"pid": 7, "started_at": 1.0, "executable": "/bin/agent"},
                health="healthy",
                report_revision=0,
                created_at=1.0,
                updated_at=1.0,
            ),
            connection=unit.connection,
        )

    cursor = daemon.store.journal.current_sequence()
    generation, _token = daemon.terminal_service.connections.acquire_callback(
        provider_id, credential
    )
    acquired = _events_after(daemon.store, cursor)
    assert [event.kind for event in acquired] == [
        "provider.updated",
        "catalog.invalidated",
        "terminal.binding_changed",
    ]
    acquired_projection = PublicParticipant.from_wire(acquired[-1].payload)
    assert acquired_projection.terminal_route is not None
    assert acquired_projection.terminal_route.health == "reconciling"

    cursor = daemon.store.journal.current_sequence()
    daemon.terminal_service.connections.renew(provider_id, generation)
    assert daemon.store.journal.current_sequence() == cursor

    terminal = {
        "provider_id": provider_id,
        "provider_generation": generation,
        "terminal_id": "event-terminal",
        "terminal_incarnation": "event-incarnation",
        "occupant": {"occupant_id": "event-occupant"},
        "process": {"pid": 7, "started_at": 1.0, "executable": "/bin/agent"},
    }
    daemon.terminal_service.report(
        provider_id,
        generation,
        1,
        {"terminals": [terminal], "complete": False},
    )
    partial = _events_after(daemon.store, cursor)
    assert [event.kind for event in partial] == [
        "provider.updated",
        "terminal.binding_changed",
    ]
    partial_projection = PublicParticipant.from_wire(partial[-1].payload)
    assert partial_projection.addressable is False
    assert partial_projection.actions["send"].route_available is False
    partial_snapshot = SnapshotService(
        daemon.store, id_factory=lambda: "partial-provider-snapshot"
    ).snapshot("partial-provider-client")
    partial_fresh = next(
        value
        for value in partial_snapshot["participants"]
        if value["participant_id"] == participant.id
    )
    assert partial_projection.to_wire() == PublicParticipant.from_wire(partial_fresh).to_wire()

    cursor = daemon.store.journal.current_sequence()
    daemon.terminal_service.report(
        provider_id,
        generation,
        2,
        {"terminals": [terminal], "complete": True},
    )
    restored = _events_after(daemon.store, cursor)
    assert [event.kind for event in restored] == [
        "provider.updated",
        "catalog.invalidated",
        "terminal.binding_changed",
    ]
    binding_event = restored[-1]
    participant_projection = PublicParticipant.from_wire(binding_event.payload)
    assert participant_projection.terminal_route is not None
    assert participant_projection.terminal_route.health == "healthy"
    assert binding_event.entity_id == participant_projection.participant_id
    assert participant_projection.extra["projection_revision"] == (
        participant_projection.owner.revision
    )
    snapshot = SnapshotService(daemon.store, id_factory=lambda: "event-participant-snapshot")
    snapshot_page = snapshot.snapshot("event-client")
    fresh_value = next(
        value
        for value in snapshot_page["participants"]
        if value["participant_id"] == participant.id
    )
    assert participant_projection.to_wire() == PublicParticipant.from_wire(fresh_value).to_wire()

    cursor = daemon.store.journal.current_sequence()
    daemon.terminal_service.connections.disconnect(provider_id, generation)
    disconnected = _events_after(daemon.store, cursor)
    assert [event.kind for event in disconnected] == [
        "provider.updated",
        "catalog.invalidated",
        "terminal.binding_changed",
    ]
    disconnected_projection = PublicParticipant.from_wire(disconnected[-1].payload)
    assert disconnected_projection.terminal_route is not None
    assert disconnected_projection.terminal_route.health == "offline"


def test_control_transfer_publishes_one_complete_transaction(daemon) -> None:
    target = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    daemon.jobs.create(
        handle="event-queued-job",
        caller_id="cli",
        target_id=target.id,
        kind="send",
        prompt="queued",
    )
    timestamp = now()
    daemon.store.reserve_control_operation(
        ControlOperation(
            operation_id="event-queued-control",
            participant_id=target.id,
            kind=ControlKind.QUEUE_FOLLOWUP,
            transport=ControlTransport.LEGACY_TMUX,
            delivery_phase=ControlDeliveryPhase.QUEUED,
            job_handle="event-queued-job",
            queue_sequence=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    cursor = daemon.store.journal.current_sequence()

    with daemon.store.write_unit() as unit:
        result = ControlTransferService(daemon).transfer(
            [{"participant_id": target.id, "expected_revision": 0}],
            {"kind": "local_operator", "participant_id": None},
            unit=unit,
        )

    assert result["cancelled_job_handles"] == ["event-queued-job"]
    groups = daemon.store.journal.groups_after(cursor, limit=10)
    assert len(groups) == 1
    assert [event.kind for event in groups[0].events] == [
        "participant.owner_changed",
        "participant.controls_changed",
        "job.updated",
    ]
    assert [event.entity_revision for event in groups[0].events] == list(
        range(groups[0].first_sequence, groups[0].ending_sequence + 1)
    )
    for event in groups[0].events[:2]:
        projected = PublicParticipant.from_wire(event.payload)
        assert projected.participant_id == event.entity_id
        assert projected.extra["projection_revision"] == projected.owner.revision
    snapshot = SnapshotService(daemon.store, id_factory=lambda: "event-control-transfer-snapshot")
    snapshot_page = snapshot.snapshot("event-client")
    fresh_value = next(
        value for value in snapshot_page["participants"] if value["participant_id"] == target.id
    )
    assert PublicParticipant.from_wire(groups[0].events[1].payload).to_wire() == (
        PublicParticipant.from_wire(fresh_value).to_wire()
    )
    assert groups[0].events[-1].payload["state"] == "killed"


async def test_restart_rollback_publishes_complete_state_group(daemon) -> None:
    participant = daemon.registry.create_spawned(harness="codex", cwd="/tmp", has_prompt=False)
    daemon.jobs.create(
        handle="event-recovery-job",
        caller_id=None,
        target_id=participant.id,
        kind="spawn",
        actor_client_id="event-client",
    )
    timestamp = now()
    operation = PublicOperationRecord(
        operation_id="event-recovery-operation",
        kind="spawn",
        actor_client_id="event-client",
        actor_participant_id=None,
        target_ids=(participant.id,),
        state="accepted",
        phase="launch_reserved",
        job_handle="event-recovery-job",
        created_at=timestamp,
        updated_at=timestamp,
    )
    with daemon.store.write_unit() as unit:
        daemon.store.operations.create(operation, connection=unit.connection)
        daemon.store.operations.reserve_launch(
            LaunchReservationRecord(
                operation_id=operation.operation_id,
                participant_id=participant.id,
                provider_id="event-provider",
                workspace_usage_id=None,
                adapter="codex",
                phase="reserved",
                launch_facts={},
                artifact_refs=(),
                dispatch_marker=None,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            connection=unit.connection,
        )
    cursor = daemon.store.journal.current_sequence()

    assert await fail_proven_undispatched(daemon, operation) is True
    groups = daemon.store.journal.groups_after(cursor, limit=10)
    assert len(groups) == 1
    events = groups[0].events
    assert [event.kind for event in events] == [
        "participant.updated",
        "job.updated",
        "operation.updated",
    ]
    assert PublicParticipant.from_wire(events[0].payload).status == "dead"
    assert PublicJob.from_wire(events[1].payload).state == "crashed"
    assert PublicOperation.from_wire(events[2].payload).state == "failed"


async def test_workspace_events_and_gc_tombstones_keep_sequence_non_reuse(
    store, tmp_path: Path
) -> None:
    ids = (f"event-value-{index}" for index in itertools.count(1))
    operations = OperationService(store, id_factory=lambda: next(ids))
    workspaces = WorkspaceService(store, operations, id_factory=lambda: next(ids))
    directory = tmp_path / "borrowed"
    directory.mkdir()
    cursor = store.journal.current_sequence()
    registered = await workspaces.register(
        client_id="event-client",
        idempotency_key="event-workspace-register",
        params={
            "ownership_kind": "frontend",
            "owner_id": "event-client",
            "path": str(directory),
        },
    )
    reservation = await workspaces.reserve(
        WorkspaceRequest(workspace_id=str(registered["workspace_id"])),
        reservation_id="event-reservation",
    )
    participant_usage = workspaces.handoff_usage(
        reservation.usage.usage_id,
        participant_id="event-workspace-participant",
    )
    workspaces.release_usage(participant_usage.usage_id, reason="verified_exit")
    workspace_events = _events_after(store, cursor)
    assert [event.kind for event in workspace_events] == [
        "workspace.updated",
        "workspace.usage_changed",
        "workspace.usage_changed",
        "workspace.usage_changed",
    ]
    for event in workspace_events[1:]:
        assert PublicWorkspace.from_wire(event.payload).workspace_id == event.entity_id
    usage_projection = PublicWorkspace.from_wire(workspace_events[-1].payload)
    assert usage_projection.workspace_id == workspace_events[-1].entity_id
    snapshot = SnapshotService(store, id_factory=lambda: "event-workspace-snapshot")
    snapshot_page = snapshot.snapshot("event-client")
    fresh_value = next(
        value
        for value in snapshot_page["workspaces"]
        if value["workspace_id"] == usage_projection.workspace_id
    )
    assert usage_projection.to_wire() == PublicWorkspace.from_wire(fresh_value).to_wire()

    old = now() - 90 * SECONDS_PER_DAY
    participant = Participant(
        id="event-gc-participant",
        harness="vibe",
        tier=Tier.EXTERNAL,
        cwd=None,
        status=Status.DEAD,
        created_at=old,
        last_activity=old,
    )
    store.upsert_participant(participant)
    store.create_job(
        Job(
            handle="event-gc-job",
            caller_id="cli",
            target_id=participant.id,
            kind="send",
            prompt="event coverage",
            state=JobState.DONE,
            result="done",
            error_code=None,
            created_at=old,
            finished_at=old,
        )
    )
    store.conn.execute(update(orchestration_events).values(recorded_at=old))
    before_gc = store.journal.current_sequence()
    await sweep(
        store,
        RetentionSection(
            bus_days=7,
            events_days=7,
            jobs_days=7,
            refused_cap=100,
            stale_running_days=7,
            batch=2,
            interval=3600.0,
            enabled=True,
        ),
    )
    tombstones = _events_after(store, before_gc)
    assert [event.kind for event in tombstones] == ["job.removed", "participant.removed"]

    last_sequence = store.journal.current_sequence()
    store.conn.execute(update(orchestration_events).values(recorded_at=old))
    assert await _sweep_journal(store, now(), batch=1) == 2
    assert store.conn.execute(select(orchestration_events.c.sequence)).all() == []
    assert store.journal.current_sequence() == last_sequence

    store.upsert_participant(
        Participant(
            id="event-after-prune",
            harness="vibe",
            tier=Tier.EXTERNAL,
            cwd=None,
        )
    )
    assert store.journal.current_sequence() == last_sequence + 1
    assert _events_after(store, last_sequence)[0].entity_id == "event-after-prune"


async def test_journal_retention_stops_at_first_unexpired_group(store) -> None:
    timestamp = now()
    recorded_at = (
        timestamp - 11 * SECONDS_PER_DAY,
        timestamp - 9 * SECONDS_PER_DAY,
        timestamp - 11 * SECONDS_PER_DAY,
    )
    with store.write_unit() as unit:
        for index, timestamp in enumerate(recorded_at, start=1):
            store.journal.append_group(
                unit,
                [
                    JournalEventRecord(
                        kind="catalog.invalidated",
                        entity_id=f"event-prefix-{index}",
                        entity_revision=index,
                        payload={"reason": "retention-prefix-test"},
                        recorded_at=timestamp,
                    )
                ],
                transaction_id=f"event-prefix-group-{index}",
            )

    await sweep(store, RetentionSection(events_days=10, batch=10))
    retained = store.journal.groups_after(1, limit=10)
    assert [group.first_sequence for group in retained] == [2, 3]
    assert [group.events[0].entity_id for group in retained] == [
        "event-prefix-2",
        "event-prefix-3",
    ]
    reader = JournalReader(store.journal)
    batch = reader.read(
        StreamCursor(store.journal.stream_id(), 1),
        limit=10,
    )
    assert [transaction["ending_cursor"]["sequence"] for transaction in batch.transactions] == [
        2,
        3,
    ]
    assert store.journal.current_sequence() == 3
