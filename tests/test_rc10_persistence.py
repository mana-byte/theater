"""RC10 transactional persistence and guarded-upgrade coverage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError

from theater.daemon.persistence import database as database_module
from theater.daemon.persistence.database import (
    HEAD,
    MIGRATIONS,
    Database,
    RC9UpgradeBlocked,
)
from theater.daemon.persistence.repositories.control_operations import (
    ControlOperation,
    ControlOperationRepository,
)
from theater.daemon.persistence.repositories.journal import JournalRepository
from theater.daemon.persistence.repositories.operations import OperationRepository
from theater.daemon.persistence.repositories.providers import ProviderRepository
from theater.daemon.persistence.repositories.terminal_bindings import TerminalBindingRepository
from theater.daemon.persistence.repositories.workspaces import WorkspaceRepository
from theater.daemon.persistence.store import Store
from theater.daemon.schema import global_scratchpad, orchestration_events, tree_kv
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
)
from theater.models import (
    IdempotencyRecord,
    JournalEventRecord,
    ProviderRecord,
    PublicOperationRecord,
    TerminalBindingRecord,
    WorkspaceRecord,
    WorkspaceUsageRecord,
)


def _upgrade(path: Path, revision: str) -> None:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as connection:
            config = Config()
            config.set_main_option("script_location", str(MIGRATIONS))
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
    finally:
        engine.dispose()


def _rc9_database(path: Path, *, live_participant: bool = False, running_job: bool = False) -> None:
    _upgrade(path, "0031")
    connection = sqlite3.connect(path)
    try:
        if live_participant:
            connection.execute(
                "INSERT INTO participants "
                "(id, harness, tier, status, last_activity, created_at) "
                "VALUES ('external-live', 'codex', 'external', 'idle', 1.0, 1.0)"
            )
        if running_job:
            connection.execute(
                "INSERT INTO jobs "
                "(handle, caller_id, kind, state, created_at) "
                "VALUES ('job-running', 'cli', 'send', 'running', 1.0)"
            )
        connection.commit()
    finally:
        connection.close()


def _logical_dump(path: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(path)
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def _provider(provider_id: str = "provider-a", selector: str = "tmux") -> ProviderRecord:
    return ProviderRecord(
        provider_id=provider_id,
        selector=selector,
        kind="terminal",
        credential_verifier="verifier",
        configuration_version=1,
        capabilities=("terminal.create",),
        limits={"input_bytes": 1024},
        generation=0,
        last_report_revision=None,
        created_at=1.0,
        updated_at=1.0,
    )


def _event(entity_id: str, revision: int = 1) -> JournalEventRecord:
    return JournalEventRecord(
        kind="provider.updated",
        entity_id=entity_id,
        entity_revision=revision,
        payload={"health": "online"},
        recorded_at=1.0,
    )


def test_fresh_and_drained_rc9_databases_migrate(tmp_path: Path) -> None:
    fresh = Database(tmp_path / "fresh.db")
    assert (
        fresh.conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == HEAD
    )
    assert {
        "global_scratchpad",
        "orchestration_events",
        "providers",
        "public_operations",
        "workspaces",
    } <= set(
        fresh.conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars()
    )
    fresh.close()

    path = tmp_path / "drained.db"
    _rc9_database(path)
    legacy = sqlite3.connect(path)
    try:
        legacy.execute(
            "INSERT INTO participants "
            "(id, harness, tier, parent_id, status, last_activity, created_at) "
            "VALUES ('child', 'codex', 'spawned', 'parent', 'dead', 1.0, 1.0)"
        )
        legacy.execute(
            "INSERT INTO participants "
            "(id, harness, tier, status, last_activity, created_at) "
            "VALUES ('root', 'vibe', 'external', 'dead', 1.0, 1.0)"
        )
        legacy.execute(
            "INSERT INTO jobs (handle, caller_id, kind, state, created_at, finished_at) "
            "VALUES ('job-old', 'parent', 'send', 'done', 1.0, 2.0)"
        )
        legacy.execute(
            "INSERT INTO tree_kv "
            "(tree_root_id, repo_root, namespace, key, value, updated_at, updated_by) "
            "VALUES ('parent', '/repo', 'notes', 'key', 'discard-me', 1.0, 'parent')"
        )
        legacy.execute(
            "INSERT INTO named_worktrees (repo_root, name, branch, path, created_at) "
            "VALUES ('/repo', 'shared', 'theater/shared', '/repo/shared', 1.0)"
        )
        legacy.commit()
    finally:
        legacy.close()

    migrated = Database(path)
    try:
        participants = migrated.conn.exec_driver_sql(
            "SELECT id, origin, control_owner_kind, control_owner_id FROM participants ORDER BY id"
        ).all()
        assert participants == [
            ("child", "spawned", "participant", "parent"),
            ("root", "external", "local_operator", None),
        ]
        assert migrated.conn.execute(select(tree_kv)).fetchall() == []
        assert migrated.conn.execute(select(global_scratchpad)).fetchall() == []
        workspace = migrated.conn.exec_driver_sql(
            "SELECT ownership_kind, path, state FROM workspaces"
        ).one()
        assert workspace == ("theater", "/repo/shared", "reconcile")
        job = migrated.conn.exec_driver_sql(
            "SELECT caller_id, actor_client_id, actor_participant_id FROM jobs"
        ).one()
        assert job == ("parent", None, None)
    finally:
        migrated.close()


def test_database_startup_refuses_live_rc9_without_touching_file(tmp_path: Path) -> None:
    path = tmp_path / "live.db"
    _rc9_database(path, live_participant=True)
    before = path.read_bytes()

    with pytest.raises(RC9UpgradeBlocked, match="external-live"):
        Database(path)

    assert path.read_bytes() == before


def test_direct_alembic_refuses_running_rc9_without_logical_changes(tmp_path: Path) -> None:
    path = tmp_path / "running.db"
    _rc9_database(path, running_job=True)
    before = _logical_dump(path)

    with pytest.raises(RC9UpgradeBlocked, match="job-running"):
        _upgrade(path, "head")

    assert _logical_dump(path) == before


def test_live_rc10_database_bypasses_drain_guard_when_head_advances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rc10-live.db"
    database = Database(path)
    database.conn.exec_driver_sql(
        "INSERT INTO participants "
        "(id, harness, tier, status, last_activity, created_at) "
        "VALUES ('rc10-live', 'codex', 'external', 'idle', 1.0, 1.0)"
    )
    database.close()

    assert database_module.revision_is_rc10("0032")
    assert not database_module.revision_is_rc10("0031")
    monkeypatch.setattr(database_module, "HEAD", "0033")

    reopened = Database(path)
    try:
        assert (
            reopened.conn.exec_driver_sql(
                "SELECT status FROM participants WHERE id = 'rc10-live'"
            ).scalar_one()
            == "idle"
        )
    finally:
        reopened.close()


def test_write_unit_rolls_back_state_and_journal_and_defers_callbacks(tmp_path: Path) -> None:
    database = Database(tmp_path / "atomic.db")
    providers = ProviderRepository(database)
    journal = JournalRepository(database)
    notifications: list[int] = []
    journal.register_listener(notifications.append)
    try:
        with pytest.raises(RuntimeError, match="abort"), database.write_unit() as unit:
            providers.register(_provider(), connection=unit.connection)
            journal.append_group(unit, [_event("provider-a")])
            raise RuntimeError("abort")

        assert providers.get("provider-a") is None
        assert journal.current_sequence() == 0
        assert database.conn.execute(select(orchestration_events)).fetchall() == []
        assert notifications == []
    finally:
        database.close()


def test_after_commit_callbacks_keep_order_and_isolate_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = Database(tmp_path / "callbacks.db")
    providers = ProviderRepository(database)
    calls: list[str] = []

    def fail() -> None:
        calls.append("failed")
        raise RuntimeError("listener broke")

    try:
        unit = database.write_unit()
        with unit:
            providers.register(_provider(), connection=unit.connection)
            unit.after_commit(lambda: calls.append("first"))
            unit.after_commit(fail)
            unit.after_commit(lambda: calls.append("last"))

        assert providers.get("provider-a") is not None
        assert calls == ["first", "failed", "last"]
        assert "after-commit notification failed" in caplog.text
        with pytest.raises(RuntimeError, match="cannot be reused"), unit:
            pass
    finally:
        database.close()


def test_uniqueness_and_atomic_workspace_handoff(tmp_path: Path) -> None:
    database = Database(tmp_path / "unique.db")
    providers = ProviderRepository(database)
    bindings = TerminalBindingRepository(database)
    operations = OperationRepository(database)
    workspaces = WorkspaceRepository(database)
    try:
        with database.write_unit() as unit:
            providers.register(_provider(), connection=unit.connection)
            workspaces.create(
                WorkspaceRecord(
                    workspace_id="workspace-a",
                    ownership_kind="frontend",
                    owner_id="regie",
                    path="/work/a",
                    state="active",
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )
            workspaces.acquire_usage(
                WorkspaceUsageRecord(
                    usage_id="usage-reservation",
                    workspace_id="workspace-a",
                    holder_kind="reservation",
                    holder_id="operation-a",
                    acquired_at=1.0,
                ),
                connection=unit.connection,
            )
            operations.create(
                PublicOperationRecord(
                    operation_id="operation-a",
                    kind="spawn",
                    actor_client_id="client-a",
                    actor_participant_id=None,
                    target_ids=("participant-a",),
                    state="accepted",
                    phase="reserved",
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )
            operations.claim_idempotency(
                IdempotencyRecord(
                    client_id="client-a",
                    key="spawn-a",
                    method="frontend.participants.spawn",
                    payload_digest="digest-a",
                    operation_id="operation-a",
                    created_at=1.0,
                ),
                connection=unit.connection,
            )
            bindings.bind(
                TerminalBindingRecord(
                    participant_id="participant-a",
                    provider_id="provider-a",
                    provider_generation=1,
                    terminal_id="terminal-a",
                    terminal_incarnation="incarnation-a",
                    occupant_evidence={"occupant_id": "occupant-a"},
                    health="healthy",
                    report_revision=1,
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )

        with pytest.raises(IntegrityError), database.write_unit() as unit:
            providers.register(_provider("provider-b", "tmux"), connection=unit.connection)
        with pytest.raises(IntegrityError), database.write_unit() as unit:
            bindings.bind(
                TerminalBindingRecord(
                    participant_id="participant-b",
                    provider_id="provider-a",
                    provider_generation=1,
                    terminal_id="terminal-a",
                    terminal_incarnation="incarnation-a",
                    occupant_evidence={"occupant_id": "occupant-a"},
                    health="healthy",
                    report_revision=1,
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )
        with pytest.raises(RuntimeError), database.write_unit() as unit:
            workspaces.handoff_usage(
                reservation_usage_id="usage-reservation",
                participant_usage=WorkspaceUsageRecord(
                    usage_id="usage-participant",
                    workspace_id="workspace-a",
                    holder_kind="participant",
                    holder_id="participant-a",
                    acquired_at=2.0,
                ),
                handed_off_at=2.0,
                connection=unit.connection,
            )
            raise RuntimeError
        assert [usage.usage_id for usage in workspaces.active_usages("workspace-a")] == [
            "usage-reservation"
        ]
    finally:
        database.close()


def test_generation_and_journal_sequence_survive_delete_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "counters.db"
    database = Database(path)
    providers = ProviderRepository(database)
    journal = JournalRepository(database)
    try:
        with database.write_unit() as unit:
            providers.register(_provider(), connection=unit.connection)
            assert (
                providers.claim_generation("provider-a", updated_at=2.0, connection=unit.connection)
                == 1
            )
            first = journal.append_group(
                unit,
                [_event("provider-a"), _event("terminal-a")],
            )
        stored_group = database.conn.execute(
            select(
                orchestration_events.c.transaction_id,
                orchestration_events.c.event_index,
                orchestration_events.c.ending_sequence,
            ).order_by(orchestration_events.c.sequence)
        ).all()
        assert stored_group == [
            (first.transaction_id, 0, first.ending_sequence),
            (first.transaction_id, 1, first.ending_sequence),
        ]
        with database.write_unit() as unit:
            journal.delete_through(first.ending_sequence, connection=unit.connection)
    finally:
        database.close()

    reopened = Database(path)
    providers = ProviderRepository(reopened)
    journal = JournalRepository(reopened)
    try:
        with reopened.write_unit() as unit:
            generation = providers.claim_generation(
                "provider-a", updated_at=3.0, connection=unit.connection
            )
            second = journal.append_group(unit, [_event("provider-a", 2)])
        assert generation == 2
        assert second.first_sequence == first.ending_sequence + 1
        assert second.stream_id == first.stream_id
    finally:
        reopened.close()


def test_global_scratchpad_schema_foundation_stores_expiry_and_audit(tmp_path: Path) -> None:
    database = Database(tmp_path / "scratchpad.db")
    try:
        with database.write_unit() as unit:
            unit.connection.execute(
                global_scratchpad.insert().values(
                    namespace="shared",
                    key="note",
                    value="foundation-only",
                    updated_at=100.0,
                    expires_at=86_500.0,
                    actor_client_id="client-a",
                    actor_participant_id="participant-a",
                )
            )
        row = database.conn.execute(select(global_scratchpad)).one()._mapping
        assert dict(row) == {
            "namespace": "shared",
            "key": "note",
            "value": "foundation-only",
            "updated_at": 100.0,
            "expires_at": 86_500.0,
            "actor_client_id": "client-a",
            "actor_participant_id": "participant-a",
        }
        indexes = {
            row[1] for row in database.conn.exec_driver_sql("PRAGMA index_list(global_scratchpad)")
        }
        assert "idx_global_scratchpad_expiry" in indexes
    finally:
        database.close()


def test_control_dispatch_persists_exact_provider_terminal_target(tmp_path: Path) -> None:
    database = Database(tmp_path / "control-target.db")
    controls = ControlOperationRepository(database)
    try:
        with database.write_unit() as unit:
            controls.reserve(
                ControlOperation(
                    operation_id="control-a",
                    participant_id="participant-a",
                    kind=ControlKind.SEND,
                    transport=ControlTransport.LEGACY_TMUX,
                    delivery_phase=ControlDeliveryPhase.RESERVED,
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )
            controls.mark_dispatched(
                "control-a",
                provider_id="provider-a",
                provider_generation=7,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                updated_at=2.0,
                connection=unit.connection,
            )

        persisted = controls.get("control-a")
        assert persisted is not None
        assert (
            persisted.provider_id,
            persisted.provider_generation,
            persisted.terminal_id,
            persisted.terminal_incarnation,
        ) == ("provider-a", 7, "terminal-a", "incarnation-a")
    finally:
        database.close()


def test_store_composes_rc10_repositories_and_provider_dispatch(tmp_path: Path) -> None:
    store = Store(tmp_path / "composed.db")
    try:
        assert isinstance(store.providers, ProviderRepository)
        assert isinstance(store.terminal_bindings, TerminalBindingRepository)
        assert isinstance(store.operations, OperationRepository)
        assert isinstance(store.workspaces, WorkspaceRepository)
        assert isinstance(store.journal, JournalRepository)

        with store.write_unit() as unit:
            store.reserve_control_operation(
                ControlOperation(
                    operation_id="control-composed",
                    participant_id="participant-a",
                    kind=ControlKind.SEND,
                    transport=ControlTransport.LEGACY_TMUX,
                    delivery_phase=ControlDeliveryPhase.RESERVED,
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )
            store.mark_control_operation_dispatched(
                "control-composed",
                provider_id="provider-a",
                provider_generation=8,
                terminal_id="terminal-a",
                terminal_incarnation="incarnation-a",
                updated_at=2.0,
                connection=unit.connection,
            )

        persisted = store.get_control_operation("control-composed")
        assert persisted is not None
        assert (
            persisted.provider_id,
            persisted.provider_generation,
            persisted.terminal_id,
            persisted.terminal_incarnation,
        ) == ("provider-a", 8, "terminal-a", "incarnation-a")
    finally:
        store.close()
