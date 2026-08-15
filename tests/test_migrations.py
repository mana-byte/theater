"""The migrations must describe the same database that `schema.py` declares.

The whole reason Alembic is here is that the pre-1.3 store had no ALTER path:
editing the schema silently did nothing to an existing database. Alembic only
fixes that if every schema edit comes with a revision, and nothing but a test
enforces that.
"""

from __future__ import annotations

import sqlite3

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, MetaData, Table, Text

from theater import paths
from theater.daemon.schema import metadata
from theater.daemon.store import HEAD, Store


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
    tables = set(store.conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).scalars())
    assert "alembic_version" in tables
    assert {"participants", "jobs", "bus", "budgets"} <= tables


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

        stamped = store.conn.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar()
        # The legacy DB is stamped at BASELINE then upgraded to head, so the
        # version is HEAD — not BASELINE, and not just "not BASELINE".
        assert stamped == HEAD
    finally:
        store.close()


def test_a_fresh_database_is_not_mistaken_for_a_legacy_one(theater_home):
    store = Store(paths.db_path())
    try:
        assert store.conn.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar() is not None
        assert _diff(store) == []
    finally:
        store.close()
