"""The migrations must describe the same database that `schema.py` declares.

The whole reason Alembic is here is that the pre-1.3 store had no ALTER path:
editing the schema silently did nothing to an existing database. Alembic only
fixes that if every schema edit comes with a revision, and nothing but a test
enforces that.
"""

from __future__ import annotations

import sqlite3

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Column, MetaData, Table, Text, create_engine

from theater import paths
from theater.daemon.schema import metadata
from theater.daemon.store import HEAD, MIGRATIONS, Store
from theater.models import Participant


def _diff(store: Store) -> list:
    context = MigrationContext.configure(
        store.conn, opts={"compare_type": True, "target_metadata": metadata}
    )
    return compare_metadata(context, metadata)


def test_head_matches_schema_module(store):
    """Edit `schema.py` without writing a revision and this fails."""
    assert _diff(store) == []


def test_the_drift_check_is_not_vacuous(store):
    """Guard the guard.

    A misconfigured `compare_metadata` returns [] for everything, which would
    make the test above pass forever and quietly restore the pre-1.3 hazard.
    Feed it metadata that is knowingly wrong and insist it notices.
    """
    drifted = MetaData()
    for table in metadata.tables.values():
        table.to_metadata(drifted)
    Table("invented", drifted, Column("id", Text, primary_key=True))

    context = MigrationContext.configure(
        store.conn, opts={"compare_type": True, "target_metadata": drifted}
    )
    assert compare_metadata(context, drifted) != []


def test_migrations_created_the_alembic_version_table(store):
    tables = set(
        store.conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").scalars()
    )
    assert "alembic_version" in tables
    assert {
        "participants",
        "jobs",
        "bus",
        "budgets",
        "tree_kv",
        "participant_artifacts",
        "named_worktrees",
        "usage",
    } <= tables
    stamped = store.conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    assert stamped == HEAD
    assert float(store.get_meta("transcript_location_epoch")) > 0

    # Migration 0009 added resume_floor to participants.
    col_info = store.conn.exec_driver_sql("PRAGMA table_info(participants)").fetchall()
    col_names = {row[1] for row in col_info}
    assert "resume_floor" in col_names
    assert "tmux_server_identity" in col_names
    assert "termination_reason" in col_names
    assert "termination_incident" in col_names
    assert "terminated_at" in col_names
    assert "resumed_from_id" in col_names

    usage_cols = store.conn.exec_driver_sql("PRAGMA table_info(usage)").fetchall()
    assert "harness" in {row[1] for row in usage_cols}
    usage_indexes = store.conn.exec_driver_sql("PRAGMA index_list(usage)").fetchall()
    assert "idx_usage_harness_ts" in {row[1] for row in usage_indexes}


def test_usage_harness_migration_backfills_survivors_and_marks_orphans(theater_home):
    path = paths.db_path()
    store = Store(path)
    participant = Participant(id="known", harness="codex")
    store.upsert_participant(participant)
    store.record_usage(
        participant_id="known",
        tree_root_id="known",
        usage_key="known-row",
        ts=1.0,
        model=None,
        harness="wrong-before-downgrade",
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        reasoning_output_tokens=0,
        cost_microcents=0,
    )
    store.close()

    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            cfg = Config()
            cfg.set_main_option("script_location", str(MIGRATIONS))
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0016")
            conn.exec_driver_sql(
                "INSERT INTO usage (participant_id, usage_key, ts) "
                "VALUES ('gone', 'orphan-row', 2.0)"
            )
            command.upgrade(cfg, "head")
            conn.commit()
            rows = conn.exec_driver_sql(
                "SELECT participant_id, harness FROM usage ORDER BY participant_id"
            ).fetchall()
    finally:
        engine.dispose()

    assert rows == [("gone", "unknown"), ("known", "codex")]


def test_tmux_restart_migration_upgrades_from_0018(theater_home):
    path = paths.db_path()
    store = Store(path)
    store.close()

    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            cfg = Config()
            cfg.set_main_option("script_location", str(MIGRATIONS))
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0018")
            before = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(participants)")}
            assert "tmux_server_identity" not in before
            command.upgrade(cfg, "head")
            conn.commit()
            after = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(participants)")}
    finally:
        engine.dispose()

    assert {
        "tmux_server_identity",
        "termination_reason",
        "termination_incident",
        "terminated_at",
        "resumed_from_id",
    } <= after


def test_resume_claim_migration_upgrades_from_0019(theater_home):
    path = paths.db_path()
    store = Store(path)
    store.close()

    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            cfg = Config()
            cfg.set_main_option("script_location", str(MIGRATIONS))
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, "0019")
            before = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(participants)")}
            assert "resumed_from_id" not in before
            command.upgrade(cfg, "head")
            conn.commit()
            after = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(participants)")}
            indexes = {row[1] for row in conn.exec_driver_sql("PRAGMA index_list(participants)")}
    finally:
        engine.dispose()

    assert "resumed_from_id" in after
    assert "uq_participants_live_resumed_from" in indexes


def test_bus_ids_are_never_reused(store):
    """AUTOINCREMENT, not bare rowid: `bus_tail(after_id=)` is a cursor."""
    first = store.bus_append("a")
    store.conn.exec_driver_sql("DELETE FROM bus")
    assert store.bus_append("b") > first


def test_a_legacy_database_is_adopted_not_rebuilt(theater_home):
    """A v1.2 file keeps its rows and gains an alembic_version of BASELINE.

    Recreating instead of stamping would make the daemon forget which pane
    belongs to which participant across the upgrade.
    """
    path = paths.db_path()
    legacy = sqlite3.connect(path, isolation_level=None)
    legacy.executescript(
        """
        CREATE TABLE participants (
            id TEXT PRIMARY KEY, harness TEXT NOT NULL, tier TEXT NOT NULL,
            tmux_pane TEXT, cwd TEXT, branch TEXT, session_id TEXT,
            parent_id TEXT, pid INTEGER, status TEXT NOT NULL,
            last_activity REAL NOT NULL, created_at REAL NOT NULL
        );
        CREATE TABLE jobs (
            handle TEXT PRIMARY KEY, caller_id TEXT NOT NULL, target_id TEXT,
            kind TEXT NOT NULL, prompt TEXT, state TEXT NOT NULL, result TEXT,
            error_code TEXT, created_at REAL NOT NULL, finished_at REAL
        );
        CREATE TABLE bus (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
            from_id TEXT, to_id TEXT, kind TEXT NOT NULL, payload TEXT
        );
        CREATE TABLE budgets (
            tree_root_id TEXT PRIMARY KEY,
            tokens INTEGER NOT NULL DEFAULT 0,
            cents INTEGER NOT NULL DEFAULT 0,
            limit_cents INTEGER
        );
        INSERT INTO participants VALUES
            ('abc', 'vibe', 'spawned', '%1', '/tmp', NULL, NULL, NULL, NULL,
             'idle', 1.0, 1.0);
        PRAGMA user_version=1;
        """
    )
    legacy.close()

    store = Store(path)
    try:
        survivor = store.get_participant("abc")
        assert survivor is not None
        assert survivor.tmux_pane == "%1"

        stamped = store.conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
        # The legacy DB is stamped at BASELINE then upgraded to head, so the
        # version is HEAD — not BASELINE, and not just "not BASELINE".
        assert stamped == HEAD

        # The legacy jobs table had no structured-result columns; the
        # migration must have added them.
        col_info = store.conn.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()
        col_names = {row[1] for row in col_info}
        assert "response_format" in col_names
        assert "structured_result" in col_names
        assert "structured_status" in col_names

        # Migration 0009 added resume_floor to participants.
        part_cols = store.conn.exec_driver_sql("PRAGMA table_info(participants)").fetchall()
        part_col_names = {row[1] for row in part_cols}
        assert "resume_floor" in part_col_names
        # Legacy rows get NULL for resume_floor — cold spawn behaviour.
        survivor = store.get_participant("abc")
        assert survivor.resume_floor is None

        # A job created on a legacy row round-trips with null structured fields.
        store.conn.exec_driver_sql(
            "INSERT INTO jobs (handle, caller_id, target_id, kind, prompt, state, "
            "result, error_code, created_at, finished_at) VALUES "
            "('legacy1', 'cli', 'p1', 'spawn', 'go', 'done', 'ok', NULL, 1.0, 2.0)"
        )
        job = store.get_job("legacy1")
        assert job is not None
        assert job.response_format is None
        assert job.structured_result is None
        assert job.structured_status is None
    finally:
        store.close()
